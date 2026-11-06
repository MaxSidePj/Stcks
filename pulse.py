"""
CompanyPulse - daily company health check.

Fetches SEC EDGAR XBRL data for every ticker in companies.txt, scores each company,
detects what changed since the last run, sends push notifications via ntfy, and writes
a mobile dashboard to docs/index.html.

Only uses the Python standard library.

Environment variables:
  SEC_CONTACT_EMAIL  required - SEC asks every client to identify itself
  NTFY_TOPIC         optional - your secret ntfy topic name (no push if empty)
  DASHBOARD_URL      optional - link opened when tapping a notification
"""
from __future__ import annotations

import html
import json
import math
import os
import sys
import time
import urllib.request
import urllib.error
from datetime import date, datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SITE = ROOT / "site"                    # dashboard, published by the workflow (not committed)
STATE_FILE = ROOT / "state" / "state.json"  # small memory between runs (committed)
LEGACY_STATE = ROOT / "docs" / "state.json"

# --------------------------------------------------------------------------- config

def load_config() -> dict:
    defaults = {
        "revenue_growth_threshold": 0.10,
        "fcf_margin_threshold": 0.10,
        "roe_threshold": 0.15,
        "max_debt_to_equity": 1.0,
        "dilution_threshold": 0.03,
        "strong_score": 70,
        "weak_score": 40,
        "notify_positive": True,
        "notify_negative": True,
        "notify_new_filings": True,
        "daily_summary": True,
    }
    path = ROOT / "config.json"
    if path.exists():
        defaults.update(json.loads(path.read_text(encoding="utf-8")))
    return defaults


def load_tickers() -> list[tuple[str, str]]:
    """Lines like `AAPL`, or for European stocks `ABI.BR = Anheuser-Busch` (name or LEI after '=')."""
    out, seen = [], set()
    for line in (ROOT / "companies.txt").read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        t, _, hint = line.partition("=")
        t = t.strip().upper()
        if t and t not in seen:
            seen.add(t)
            out.append((t, hint.strip()))
    return out

# --------------------------------------------------------------------------- SEC

def sec_get(url: str) -> bytes:
    email = os.environ.get("SEC_CONTACT_EMAIL", "").strip()
    if "@" not in email:
        sys.exit("Set SEC_CONTACT_EMAIL (SEC requires a contact email in the User-Agent).")
    req = urllib.request.Request(url, headers={
        "User-Agent": f"CompanyPulse/1.0 {email}",
        "Accept": "application/json",
    })
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503) and attempt < 2:
                time.sleep(2 + attempt * 3)
                continue
            raise
        except urllib.error.URLError:
            if attempt < 2:
                time.sleep(3)
                continue
            raise
    raise RuntimeError("unreachable")


def ticker_map() -> dict[str, dict]:
    raw = json.loads(sec_get("https://www.sec.gov/files/company_tickers.json"))
    out = {}
    for v in raw.values():
        out.setdefault(v["ticker"].upper(), {"cik": int(v["cik_str"]), "name": v["title"]})
    return out

# --------------------------------------------------------------------------- parsing

C = lambda *names: [tuple(n.split(":")) for n in names]  # noqa: E731

REVENUE = C("us-gaap:Revenues", "us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax",
            "us-gaap:RevenueFromContractWithCustomerIncludingAssessedTax", "us-gaap:SalesRevenueNet",
            "us-gaap:SalesRevenueGoodsNet", "ifrs-full:Revenue", "ifrs-full:RevenueFromContractsWithCustomers")
GROSS = C("us-gaap:GrossProfit", "ifrs-full:GrossProfit")
OPINC = C("us-gaap:OperatingIncomeLoss", "ifrs-full:ProfitLossFromOperatingActivities")
NETINC = C("us-gaap:NetIncomeLoss", "us-gaap:NetIncomeLossAvailableToCommonStockholdersBasic", "us-gaap:ProfitLoss",
           "ifrs-full:ProfitLossAttributableToOwnersOfParent", "ifrs-full:ProfitLoss")
SHARES = C("us-gaap:WeightedAverageNumberOfDilutedSharesOutstanding",
           "ifrs-full:AdjustedWeightedAverageShares", "ifrs-full:WeightedAverageShares")
OCF = C("us-gaap:NetCashProvidedByUsedInOperatingActivities",
        "us-gaap:NetCashProvidedByUsedInOperatingActivitiesContinuingOperations",
        "ifrs-full:CashFlowsFromUsedInOperatingActivities")
CAPEX = C("us-gaap:PaymentsToAcquirePropertyPlantAndEquipment", "us-gaap:PaymentsToAcquireProductiveAssets",
          "ifrs-full:PurchaseOfPropertyPlantAndEquipmentClassifiedAsInvestingActivities",
          "ifrs-full:PurchaseOfPropertyPlantAndEquipment")
CASH = C("us-gaap:CashAndCashEquivalentsAtCarryingValue",
         "us-gaap:CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents", "ifrs-full:CashAndCashEquivalents")
DEBT = C("us-gaap:LongTermDebt", "us-gaap:LongTermDebtNoncurrent",
         "ifrs-full:Borrowings", "ifrs-full:NoncurrentPortionOfNoncurrentBorrowings")
EQUITY = C("us-gaap:StockholdersEquity", "us-gaap:StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",
           "ifrs-full:EquityAttributableToOwnersOfParent", "ifrs-full:Equity")

DIVIDENDS = C("us-gaap:PaymentsOfDividendsCommonStock", "us-gaap:PaymentsOfDividends",
              "ifrs-full:DividendsPaidClassifiedAsFinancingActivities")
BUYBACKS = C("us-gaap:PaymentsForRepurchaseOfCommonStock", "ifrs-full:PaymentsToAcquireOrRedeemEntitysShares")

FORMS = {"10-K", "10-K/A", "10-Q", "10-Q/A", "20-F", "20-F/A", "40-F", "40-F/A"}
ANNUAL_FORMS = {"10-K", "10-K/A", "20-F", "20-F/A", "40-F", "40-F/A"}


def d(s: str | None) -> date | None:
    try:
        return date.fromisoformat(s) if s else None
    except ValueError:
        return None


def series(facts: dict, concepts, unit: str, kind: str) -> dict[date, dict]:
    """One value per period end date; concepts merged in priority order; latest filing wins."""
    merged: dict[date, dict] = {}
    for tax, name in concepts:
        entries = facts.get(tax, {}).get(name, {}).get("units", {}).get(unit)
        if not entries:
            continue
        own: dict[date, dict] = {}
        for e in entries:
            form, val, end, filed = e.get("form"), e.get("val"), d(e.get("end")), d(e.get("filed"))
            if form not in FORMS or not isinstance(val, (int, float)) or not end or not filed:
                continue
            start = d(e.get("start"))
            if kind == "instant":
                if start is not None or form not in ANNUAL_FORMS:
                    continue
            else:
                if start is None:
                    continue
                days = (end - start).days
                if not (340 <= days <= 380 if kind == "annual" else 80 <= days <= 100):
                    continue
            if end in own and own[end]["filed"] >= filed:
                continue
            own[end] = {"start": start, "end": end, "val": float(val), "form": form,
                        "filed": filed, "accn": e.get("accn", "")}
        for k, v in own.items():
            merged.setdefault(k, v)
    return merged


def lookup(s: dict[date, dict], end: date) -> float | None:
    if end in s:
        return s[end]["val"]
    near = [p for p in s.values() if abs((p["end"] - end).days) <= 7]
    return min(near, key=lambda p: abs((p["end"] - end).days))["val"] if near else None


def parse(raw: dict) -> dict:
    facts = raw.get("facts", {})
    latest: dict[str, str] = {}
    for tax, name in REVENUE + NETINC:
        for unit, entries in facts.get(tax, {}).get(name, {}).get("units", {}).items():
            if len(unit) == 3 and unit.isupper():
                latest[unit] = max([latest.get(unit, "")] + [e.get("end", "") for e in entries])
    # the reporting currency is the one with the most recent data; USD wins ties
    cur = max(latest, key=lambda u: (latest[u], u == "USD")) if latest else "USD"

    revA, niA = series(facts, REVENUE, cur, "annual"), series(facts, NETINC, cur, "annual")
    gpA, opA = series(facts, GROSS, cur, "annual"), series(facts, OPINC, cur, "annual")
    shA = series(facts, SHARES, "shares", "annual")
    ocfA, capA = series(facts, OCF, cur, "annual"), series(facts, CAPEX, cur, "annual")
    cashI, debtI, eqI = (series(facts, x, cur, "instant") for x in (CASH, DEBT, EQUITY))
    divA, bbA = series(facts, DIVIDENDS, cur, "annual"), series(facts, BUYBACKS, cur, "annual")

    annual = []
    for end in sorted(revA or niA)[-10:]:
        src = revA.get(end) or niA.get(end)
        p = {"start": src["start"], "end": end,
             "revenue": lookup(revA, end), "net_income": lookup(niA, end),
             "gross_profit": lookup(gpA, end), "operating_income": lookup(opA, end),
             "shares": lookup(shA, end), "ocf": lookup(ocfA, end), "capex": lookup(capA, end),
             "cash": lookup(cashI, end), "debt": lookup(debtI, end), "equity": lookup(eqI, end),
             "dividends": lookup(divA, end), "buybacks": lookup(bbA, end)}
        p["fcf"] = p["ocf"] - abs(p["capex"] or 0) if p["ocf"] is not None else None
        annual.append(p)

    revQ, niQ = series(facts, REVENUE, cur, "quarter"), series(facts, NETINC, cur, "quarter")
    quarterly = [{"start": (revQ.get(e) or niQ.get(e))["start"], "end": e,
                  "revenue": lookup(revQ, e), "net_income": lookup(niQ, e)}
                 for e in sorted(revQ or niQ)]
    # 10-Ks don't report Q4 on its own: Q4 = full year - (Q1+Q2+Q3)
    for y in annual:
        if any(abs((q["end"] - y["end"]).days) <= 7 for q in quarterly):
            continue
        inside = [q for q in quarterly if q["start"] and (q["start"] - y["start"]).days >= -7
                  and (y["end"] - q["end"]).days >= 30]
        if len(inside) != 3:
            continue
        q4 = {"start": None, "end": y["end"]}
        for k in ("revenue", "net_income"):
            vals = [q[k] for q in inside]
            q4[k] = y[k] - sum(vals) if y[k] is not None and None not in vals else None
        quarterly.append(q4)
    quarterly = sorted(quarterly, key=lambda q: q["end"])[-12:]

    latest = max([*revA.values(), *niA.values(), *revQ.values(), *niQ.values()],
                 key=lambda p: (p["filed"], p["end"]), default=None)
    filing = {"accn": latest["accn"], "form": latest["form"], "filed": latest["filed"].isoformat(),
              "period_end": latest["end"].isoformat()} if latest else None
    return {"currency": cur, "annual": annual, "quarterly": quarterly, "filing": filing}

# --------------------------------------------------------------------------- formatting

def pct(v, signed=False):
    if v is None or not math.isfinite(v):
        return "–"
    return f"{v * 100:+.1f}%" if signed else f"{v * 100:.1f}%"


def money(v, cur="USD"):
    if v is None or not math.isfinite(v):
        return "–"
    sym = {"USD": "$", "EUR": "€", "GBP": "£", "JPY": "¥"}.get(cur, cur + " ")
    a = abs(v)
    for div, suf in ((1e12, "T"), (1e9, "B"), (1e6, "M"), (1e3, "K")):
        if a >= div:
            return f"{'-' if v < 0 else ''}{sym}{a / div:.{1 if suf in 'MK' else 2}f}{suf}"
    return f"{'-' if v < 0 else ''}{sym}{a:.2f}"


def ratio(v):
    return "–" if v is None else f"{v:.2f}×"

# --------------------------------------------------------------------------- analysis

def growth(new, old):
    return new / old - 1 if new is not None and old is not None and old > 0 else None


def margin(v, rev):
    return v / rev if v is not None and rev and rev > 0 else None


def analyze(f: dict, cfg: dict) -> dict:
    a, q, cur = f["annual"], f["quarterly"], f["currency"]
    signals = []

    def add(sid, cat, tone, title, detail, weight):
        signals.append({"id": sid, "cat": cat, "tone": tone, "title": title, "detail": detail, "weight": weight})

    if not a:
        return {"score": 50, "rating": "Mixed", "signals": [], "metrics": {}}
    c = a[-1]
    p = a[-2] if len(a) >= 2 else {}
    b3 = a[-4] if len(a) >= 4 else {}

    m = {
        "revenue": c["revenue"],
        "revenue_growth": growth(c["revenue"], p.get("revenue")),
        "cagr3": (c["revenue"] / b3["revenue"]) ** (1 / 3) - 1
        if c["revenue"] and b3.get("revenue") and c["revenue"] > 0 and b3["revenue"] > 0 else None,
        "ni_growth": growth(c["net_income"], p.get("net_income")),
        "gross_margin": margin(c["gross_profit"], c["revenue"]),
        "op_margin": margin(c["operating_income"], c["revenue"]),
        "net_margin": margin(c["net_income"], c["revenue"]),
        "fcf": c["fcf"],
        "fcf_margin": margin(c["fcf"], c["revenue"]),
        "fcf_growth": growth(c["fcf"], p.get("fcf")),
        "cash_conv": c["ocf"] / c["net_income"] if c["ocf"] is not None and c["net_income"] and c["net_income"] > 0 else None,
        "roe": c["net_income"] / c["equity"] if c["net_income"] is not None and c["equity"] and c["equity"] > 0 else None,
        "de": (c["debt"] or 0) / c["equity"] if c["equity"] and c["equity"] > 0 else None,
        "net_cash": c["cash"] - (c["debt"] or 0) if c["cash"] is not None else None,
        "share_change": growth(c["shares"], p.get("shares")),
    }
    prev_gm = margin(p.get("gross_profit"), p.get("revenue"))
    prev_om = margin(p.get("operating_income"), p.get("revenue"))

    streak = 0
    for i in range(len(a) - 1, 0, -1):
        if a[i]["revenue"] and a[i - 1]["revenue"] and a[i]["revenue"] > a[i - 1]["revenue"]:
            streak += 1
        else:
            break

    def year_ago(per):
        cands = [x for x in q if abs((per["end"] - x["end"]).days - 365) <= 20]
        return min(cands, key=lambda x: abs((per["end"] - x["end"]).days - 365)) if cands else None

    lq = pq = None
    if q:
        ya = year_ago(q[-1])
        lq = growth(q[-1]["revenue"], ya["revenue"] if ya else None)
        if len(q) >= 2:
            ya2 = year_ago(q[-2])
            pq = growth(q[-2]["revenue"], ya2["revenue"] if ya2 else None)
    m["q_growth"] = lq

    t = cfg
    g = m["revenue_growth"]
    if g is not None:
        if g >= t["revenue_growth_threshold"]:
            add("rev_growth_strong", "Growth", "pos", "Revenue growing fast", f"Revenue {pct(g, True)} YoY to {money(c['revenue'], cur)}.", 12)
        elif g >= 0.03:
            add("rev_growth", "Growth", "pos", "Revenue growing", f"Revenue {pct(g, True)} YoY.", 5)
        elif g < 0:
            add("rev_decline", "Growth", "neg", "Revenue declining", f"Revenue {pct(g, True)} YoY.", -12)
    if m["cagr3"] is not None and m["cagr3"] >= t["revenue_growth_threshold"]:
        add("rev_cagr", "Growth", "pos", "Sustained multi-year growth", f"Revenue compounded {pct(m['cagr3'])}/yr over 3 years.", 8)
    if streak >= 3:
        add("rev_streak", "Growth", "pos", f"{streak} straight years of growth", f"Revenue rose every year for {streak} years.", 5)
    if lq is not None:
        if pq is not None and lq > 0 and lq > pq + 0.02:
            add("growth_accel", "Growth", "pos", "Growth accelerating", f"Latest quarter {pct(lq, True)} YoY vs {pct(pq, True)} the quarter before.", 6)
        elif pq is not None and lq >= 0 and lq < pq - 0.05:
            add("growth_slowing", "Growth", "warn", "Growth slowing", f"Latest quarter {pct(lq, True)} YoY vs {pct(pq, True)} the quarter before.", -4)
        if lq < 0:
            add("q_rev_down", "Growth", "warn", "Latest quarter revenue down", f"Revenue {pct(lq, True)} vs same quarter last year.", -6)

    ni = c["net_income"]
    if ni is not None:
        if ni < 0:
            add("net_loss", "Profitability", "neg", "Losing money", f"Net loss of {money(ni, cur)} last fiscal year.", -10)
        elif p.get("net_income") is not None and p["net_income"] <= 0:
            add("turned_profitable", "Profitability", "pos", "Turned profitable", f"Net income {money(ni, cur)} after a loss the year before.", 10)
    if m["ni_growth"] is not None and m["ni_growth"] >= t["revenue_growth_threshold"]:
        add("earnings_growth", "Profitability", "pos", "Earnings growing", f"Net income {pct(m['ni_growth'], True)} YoY.", 6)
    gm = m["gross_margin"]
    if gm is not None and prev_gm is not None:
        if gm - prev_gm > 0.01:
            add("gm_up", "Profitability", "pos", "Gross margin expanding", f"Gross margin {pct(prev_gm)} → {pct(gm)}.", 5)
        elif gm - prev_gm < -0.02:
            add("gm_down", "Profitability", "warn", "Gross margin shrinking", f"Gross margin {pct(prev_gm)} → {pct(gm)}.", -5)
    om = m["op_margin"]
    if om is not None:
        if om >= 0.20:
            add("high_om", "Profitability", "pos", "High operating margin", f"{pct(om)} of revenue becomes operating profit.", 6)
        if prev_om is not None:
            if om - prev_om > 0.01:
                add("op_leverage", "Profitability", "pos", "Operating leverage", f"Operating margin {pct(prev_om)} → {pct(om)}.", 5)
            elif om - prev_om < -0.02:
                add("om_down", "Profitability", "warn", "Operating margin shrinking", f"Operating margin {pct(prev_om)} → {pct(om)}.", -5)

    fcf = m["fcf"]
    if fcf is not None:
        if fcf < 0:
            add("cash_burn", "Cash flow", "neg", "Burning cash", f"Free cash flow {money(fcf, cur)}.", -12)
        elif m["fcf_margin"] is not None and m["fcf_margin"] >= t["fcf_margin_threshold"]:
            add("fcf_strong", "Cash flow", "pos", "Strong free cash flow", f"{money(fcf, cur)} FCF ({pct(m['fcf_margin'])} of revenue).", 12)
        else:
            add("fcf_positive", "Cash flow", "pos", "Free cash flow positive", f"{money(fcf, cur)} FCF last fiscal year.", 5)
    if m["fcf_growth"] is not None and m["fcf_growth"] >= t["revenue_growth_threshold"]:
        add("fcf_growth", "Cash flow", "pos", "Free cash flow growing", f"FCF {pct(m['fcf_growth'], True)} YoY.", 6)
    cc = m["cash_conv"]
    if cc is not None:
        if cc >= 1.0:
            add("quality_earnings", "Cash flow", "pos", "Earnings backed by cash", f"Operating cash flow is {ratio(cc)} net income.", 4)
        elif cc < 0.7:
            add("weak_cash_conv", "Cash flow", "warn", "Earnings not backed by cash", f"Operating cash flow only {ratio(cc)} net income.", -6)

    if m["net_cash"] is not None and m["net_cash"] > 0:
        add("net_cash", "Balance sheet", "pos", "Net cash position", f"Cash exceeds long-term debt by {money(m['net_cash'], cur)}.", 5)
    if c["equity"] is not None and c["equity"] <= 0:
        add("neg_equity", "Balance sheet", "warn", "Negative shareholders' equity", "Liabilities exceed assets (can be caused by big buybacks).", -6)
    elif m["de"] is not None and m["de"] > t["max_debt_to_equity"]:
        add("high_leverage", "Balance sheet", "warn", "High leverage", f"Long-term debt is {ratio(m['de'])} equity.", -8)
    if m["roe"] is not None and m["roe"] >= t["roe_threshold"]:
        add("high_roe", "Balance sheet", "pos", "High return on equity", f"ROE {pct(m['roe'])}.", 6)

    sc = m["share_change"]
    if sc is not None:
        if sc <= -0.01:
            add("buybacks", "Shareholders", "pos", "Share count shrinking", f"Diluted shares {pct(sc, True)} (buybacks).", 4)
        elif sc >= t["dilution_threshold"]:
            add("dilution", "Shareholders", "warn", "Shareholder dilution", f"Diluted shares {pct(sc, True)} YoY.", -6)

    raw = sum(s["weight"] for s in signals)
    score = max(0, min(100, 50 + round(raw * 0.45)))
    rating = "Strong" if score >= 70 else "Healthy" if score >= 55 else "Mixed" if score >= 40 else "Weak"
    signals.sort(key=lambda s: -abs(s["weight"]))
    return {"score": score, "rating": rating, "signals": signals, "metrics": m}

# --------------------------------------------------------------------------- change detection

def detect_changes(ticker: str, prev: dict | None, cur: dict, f: dict, cfg: dict) -> list[dict]:
    if not prev:  # first time we see this company: baseline only
        return []
    out = []
    now = datetime.now(timezone.utc).isoformat(timespec="minutes")

    def ev(tone, title, body):
        out.append({"date": now, "ticker": ticker, "tone": tone, "title": title, "body": body})

    filing = f["filing"]
    if cfg["notify_new_filings"] and filing and filing["accn"] != prev.get("filing"):
        m = cur["metrics"]
        parts = []
        if filing["form"].startswith("10-Q") and m.get("q_growth") is not None:
            parts.append(f"Quarterly revenue {pct(m['q_growth'], True)} YoY")
        elif m.get("revenue_growth") is not None:
            parts.append(f"Revenue {pct(m['revenue_growth'], True)} YoY")
        if m.get("fcf") is not None:
            parts.append(f"FCF {money(m['fcf'], f['currency'])}")
        parts.append(f"Score {prev.get('score')} → {cur['score']}")
        ev("info", f"New {filing['form']} filed", " · ".join(parts))

    old_ids = set(prev.get("signals", []))
    for s in cur["signals"]:
        if s["id"] in old_ids:
            continue
        if (s["tone"] == "pos" and cfg["notify_positive"]) or (s["tone"] in ("neg", "warn") and cfg["notify_negative"]):
            ev(s["tone"], s["title"], s["detail"])

    ps = prev.get("score")
    if ps is not None:
        if cfg["notify_positive"] and ps < cfg["strong_score"] <= cur["score"]:
            ev("pos", "Health score is now Strong", f"Score rose {ps} → {cur['score']}.")
        if cfg["notify_negative"] and ps > cfg["weak_score"] >= cur["score"]:
            ev("neg", "Health score dropped to Weak", f"Score fell {ps} → {cur['score']}.")
    return out

# --------------------------------------------------------------------------- push (ntfy)

def push(title: str, message: str, tags: list[str], priority: int = 3):
    topic = os.environ.get("NTFY_TOPIC", "").strip()
    if not topic:
        return
    payload = {"topic": topic, "title": title, "message": message, "tags": tags, "priority": priority}
    if os.environ.get("DASHBOARD_URL"):
        payload["click"] = os.environ["DASHBOARD_URL"]
    req = urllib.request.Request("https://ntfy.sh/", data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        urllib.request.urlopen(req, timeout=20).read()
    except Exception as e:  # never fail the whole run because of a push
        print(f"  push failed: {e}")

# --------------------------------------------------------------------------- dashboard

TONE_ICON = {"pos": "▲", "neg": "▼", "warn": "!", "info": "◆"}


def score_class(s):
    return "strong" if s >= 70 else "healthy" if s >= 55 else "mixed" if s >= 40 else "weak"


def sign_class(v):
    return "" if v is None else "up" if v > 0.005 else "down" if v < -0.005 else ""


def bar_svg(points: list[tuple[str, float | None, float | None]], cur: str) -> str:
    """Grouped bars: revenue (blue) and free cash flow (green)."""
    vals = [v for _, a, b in points for v in (a, b) if v is not None]
    if not vals:
        return ""
    hi, lo = max(max(vals), 0), min(min(vals), 0)
    span = (hi - lo) or 1
    W, H, pad = 320, 140, 18
    n = len(points)
    slot = W / n
    bw = slot * 0.32
    zero = pad + (hi / span) * (H - 2 * pad)
    parts = [f'<line x1="0" x2="{W}" y1="{zero:.1f}" y2="{zero:.1f}" class="axis"/>']
    for i, (label, a, b) in enumerate(points):
        x0 = i * slot + slot * 0.16
        for j, (v, cls) in enumerate(((a, "rev"), (b, "fcf"))):
            if v is None:
                continue
            h = abs(v) / span * (H - 2 * pad)
            y = zero - h if v >= 0 else zero
            parts.append(f'<rect x="{x0 + j * bw:.1f}" y="{y:.1f}" width="{bw - 2:.1f}" height="{max(h, 1):.1f}" rx="2" class="{cls}"><title>{label}: {money(v, cur)}</title></rect>')
        parts.append(f'<text x="{i * slot + slot / 2:.1f}" y="{H - 2}" class="lbl">{label}</text>')
    return f'<svg viewBox="0 0 {W} {H}" class="chart">{"".join(parts)}</svg>'


def spark_svg(values: list[float]) -> str:
    values = [v for v in values if v is not None]
    if len(values) < 2:
        return ""
    lo, hi = min(values), max(values)
    span = (hi - lo) or 1
    W, H = 70, 26
    pts = " ".join(f"{i * W / (len(values) - 1):.1f},{H - 2 - (v - lo) / span * (H - 4):.1f}" for i, v in enumerate(values))
    cls = "up" if values[-1] >= values[0] else "down"
    return f'<svg viewBox="0 0 {W} {H}" class="spark {cls}"><polyline points="{pts}"/></svg>'




CSS = """
:root{--bg:#f2f2f7;--card:#fff;--fg:#111;--mut:#6b7280;--line:#e5e7eb;--pos:#16a34a;--neg:#dc2626;--warn:#d97706;--info:#2563eb}
@media (prefers-color-scheme:dark){:root{--bg:#0b0f14;--card:#161b22;--fg:#f3f4f6;--mut:#9ca3af;--line:#2a313b}}
*{box-sizing:border-box}body{margin:0;font:15px/1.4 -apple-system,system-ui,sans-serif;background:var(--bg);color:var(--fg);
padding:env(safe-area-inset-top) 12px 40px}
header{padding:18px 4px 4px}h1{font-size:28px;margin:0}header small{color:var(--mut)}
.tabs{position:sticky;top:0;z-index:5;display:flex;gap:4px;padding:8px 0;background:var(--bg)}
.tabs button{flex:1;border:0;border-radius:10px;padding:8px 2px;font:600 13px -apple-system,system-ui;background:var(--card);color:var(--mut)}
.tabs button.on{background:var(--info);color:#fff}
h2{font-size:13px;text-transform:uppercase;letter-spacing:.05em;color:var(--mut);margin:22px 6px 2px}
.desc{color:var(--mut);font-size:13px;margin:0 6px 8px}
.card,.alerts{background:var(--card);border-radius:14px;margin-bottom:10px;overflow:hidden}
summary{list-style:none;display:flex;gap:12px;align-items:center;padding:12px;cursor:pointer}summary::-webkit-details-marker{display:none}
.ring{--c:var(--mut);flex:none;width:46px;height:46px;border-radius:50%;display:grid;place-items:center;
background:conic-gradient(var(--c) calc(var(--p)*1%),var(--line) 0)}
.ring span{width:36px;height:36px;border-radius:50%;background:var(--card);display:grid;place-items:center;font-weight:700;color:var(--c)}
.strong{--c:var(--pos);color:var(--pos)}.healthy{--c:#0d9488;color:#0d9488}.mixed{--c:var(--warn);color:var(--warn)}.weak{--c:var(--neg);color:var(--neg)}
.who{flex:1;min-width:0}.who small{color:var(--mut);display:block;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.who em{font-style:normal;font-size:12px;font-weight:600}.who .inl{display:inline;font-size:12px}
.badge{font-size:10px;font-weight:700;padding:1px 6px;border-radius:99px;background:var(--info);color:#fff;vertical-align:2px}
.chips{display:flex;gap:4px;margin-top:4px;flex-wrap:wrap}.chip{font-size:11px;padding:1px 7px;border-radius:99px;background:var(--line)}
.up{color:var(--pos)}.down{color:var(--neg)}
.spark{width:70px;height:26px;flex:none;fill:none;stroke-width:2}.spark.up{stroke:var(--pos)}.spark.down{stroke:var(--neg)}
.body{padding:0 14px 14px;border-top:1px solid var(--line)}h3{font-size:14px;margin:16px 0 8px}
ul{list-style:none;margin:0;padding:0}.sigs li,.alerts li{padding:8px 0;border-bottom:1px solid var(--line)}
.sigs li:last-child,.alerts li:last-child{border:0}
li b{display:block;font-size:14px}li span{color:var(--mut);font-size:13px}li time{display:block;font-size:11px;color:var(--mut)}
li.pos b{color:var(--pos)}li.neg b{color:var(--neg)}li.warn b{color:var(--warn)}li.info b{color:var(--info)}
.alerts{padding:4px 14px}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:10px}.grid small{display:block;color:var(--mut);font-size:12px}
.chart{width:100%;height:auto}.chart .rev{fill:#3b82f6}.chart .fcf{fill:#22c55e}.chart .axis{stroke:var(--line)}
.chart .lbl{font-size:9px;fill:var(--mut);text-anchor:middle}
.dot{display:inline-block;width:9px;height:9px;border-radius:2px}.dot.rev{background:#3b82f6}.dot.fcf{background:#22c55e}
.foot{font-size:12px;color:var(--mut)}a{color:var(--info)}.err{color:var(--neg);font-size:13px}
.filters{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin:10px 0}
.filters input,.filters select{width:100%;padding:9px;border-radius:10px;border:1px solid var(--line);background:var(--card);color:var(--fg);font-size:16px}
.filters .full{grid-column:1/-1}.filters label{font-size:12px;color:var(--mut)}
.empty{color:var(--mut);padding:20px 6px}
"""

EXTRA_CSS = """
.tabs{overflow-x:auto;scrollbar-width:none;-webkit-overflow-scrolling:touch;gap:6px}.tabs::-webkit-scrollbar{display:none}
.tabs button{flex:0 0 auto;padding:8px 14px;white-space:nowrap}
.regions{display:flex;gap:6px;overflow-x:auto;margin:4px 0 10px}.regions button{border:0;border-radius:99px;padding:5px 12px;
font:600 12px -apple-system,system-ui;background:var(--card);color:var(--mut)}.regions button.on{background:var(--fg);color:var(--bg)}
.rank{font-weight:800;color:var(--mut);font-size:13px;min-width:22px}
.status{font-size:11px;font-weight:800;padding:2px 8px;border-radius:99px;color:#fff;vertical-align:2px}
.st-strong{background:#15803d}.st-buy{background:#22c55e}.st-watch{background:#d97706}.st-avoid{background:#9ca3af}.st-deal{background:#6366f1}
.factors{display:grid;grid-template-columns:repeat(4,1fr);gap:6px;margin:12px 0}
.factors div{background:var(--line);border-radius:10px;padding:6px;text-align:center}.factors small{display:block;font-size:11px;color:var(--mut)}
.factors b{font-size:16px}
.why li{padding:4px 0 4px 18px;position:relative;font-size:14px}.why li:before{content:"✓";position:absolute;left:0;color:var(--pos)}
.risk li{padding:4px 0 4px 18px;position:relative;font-size:14px}.risk li:before{content:"!";position:absolute;left:3px;color:var(--warn);font-weight:800}
.news li{padding:6px 0;border-bottom:1px solid var(--line);font-size:13px}.news li:last-child{border:0}.news time{color:var(--mut);font-size:11px}
.flag{font-size:10px;font-weight:700;padding:1px 6px;border-radius:99px;margin-left:4px}
.flag.deal{background:#6366f1;color:#fff}.flag.neg{background:var(--neg);color:#fff}.flag.pos{background:var(--pos);color:#fff}
.links{display:flex;flex-wrap:wrap;gap:6px;margin-top:12px}.links a{background:var(--line);border-radius:99px;padding:5px 11px;font-size:13px;text-decoration:none}
.dca{border-radius:12px;padding:10px 12px;margin-top:12px;background:var(--line)}
.dca b{display:block;font-size:16px}.dca span{font-size:13px;color:var(--mut)}
.dca.strong{background:rgba(22,163,74,.15)}.dca.weak{background:rgba(220,38,38,.12)}.dca.mixed{background:rgba(217,119,6,.12)}
.overview{background:var(--card);border-radius:14px;padding:4px 14px;margin-bottom:10px}
.overview div{display:flex;gap:8px;align-items:center;padding:7px 0;border-bottom:1px solid var(--line);font-size:14px}
.overview div:last-child{border:0}.overview b{width:70px}.overview .grow{flex:1;color:var(--mut);font-size:13px}
.method{background:var(--card);border-radius:14px;padding:4px 16px 14px;font-size:14px}.method h3{margin-top:18px}
.method table{width:100%;border-collapse:collapse;font-size:13px}.method td{padding:5px 4px;border-bottom:1px solid var(--line);vertical-align:top}
"""

JS = r"""
const $=s=>document.querySelector(s);
function tab(t){if(!document.getElementById(t))t=document.querySelector('.tabs button').dataset.t;
 document.querySelectorAll('.tabs button').forEach(b=>{b.classList.toggle('on',b.dataset.t===t);if(b.dataset.t===t)b.scrollIntoView({inline:'center',block:'nearest'});});
 document.querySelectorAll('main>section').forEach(s=>s.hidden=s.id!==t);history.replaceState(null,'','#'+t);window.scrollTo(0,0);
 if(t==='all')loadAll();}
document.querySelectorAll('.tabs button').forEach(b=>b.onclick=()=>tab(b.dataset.t));
document.querySelectorAll('.regions').forEach(bar=>bar.querySelectorAll('button').forEach(b=>b.onclick=()=>{
 bar.querySelectorAll('button').forEach(x=>x.classList.toggle('on',x===b));const r=b.dataset.r;
 bar.parentElement.querySelectorAll('details.card').forEach(c=>c.hidden=r!=='All'&&c.dataset.region!==r);}));
let U=null;
async function loadAll(){if(U)return;$('#list').innerHTML='<p class="empty">Loading…</p>';
 U=await (await fetch('universe.json')).json();
 const secs=[...new Set(U.rows.map(r=>r[13]))].sort();
 $('#sec').innerHTML='<option value="">All sectors</option>'+secs.map(s=>`<option>${esc(s)}</option>`).join('');draw();}
const esc=s=>String(s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const pct=(v,s)=>v==null?'–':(s&&v>0?'+':'')+(v*100).toFixed(1)+'%';
const cls=v=>v==null?'':v>0.005?'up':v<-0.005?'down':'';
const sc=s=>s>=70?'strong':s>=55?'healthy':s>=40?'mixed':'weak';
const vc=l=>l==='Attractive'?'up':l==='Expensive'?'down':'';
const stc={'Strong buy':'st-strong','Buy':'st-buy','Watch':'st-watch','Avoid':'st-avoid','Deal pending':'st-deal'};
function money(v){if(v==null)return'–';const a=Math.abs(v),g=v<0?'-$':'$';
 return a>=1e12?g+(a/1e12).toFixed(2)+'T':a>=1e9?g+(a/1e9).toFixed(2)+'B':g+(a/1e6).toFixed(0)+'M';}
function num(id,div){const v=$(id).value;return v===''?-Infinity:+v/div;}
function row(r){
 const sigs=r[12].map(i=>`<li class="${U.sig[i][0]}"><b>${esc(U.sig[i][1])}</b></li>`).join('')||'<li>No notable signals.</li>';
 return `<details class="card"><summary><div class="ring ${sc(r[21])}" style="--p:${r[21]}"><span>${r[21]}</span></div>
 <div class="who"><div><b>${esc(r[0])}</b> ${r[22]?`<span class="status ${stc[r[22]]}">${r[22]}</span>`:''} ${r[16]?`<em class="${vc(r[16])}">${r[16]}</em>`:''}
 <small class="inl">${esc(r[20])} · ${esc(r[13])} · ${money(r[18])}</small></div><small>${esc(r[1])}</small>
 <div class="chips"><span class="chip">Health ${r[3]}</span><span class="chip ${cls(r[5])}">Rev ${pct(r[5],1)}</span>
 ${r[7]!=null?`<span class="chip ${cls(r[7])}">Qtr ${pct(r[7],1)}</span>`:''}${r[15]!=null?`<span class="chip ${cls(r[15])}">FCF yld ${pct(r[15])}</span>`:''}
 ${r[14]!=null?`<span class="chip">P/E ${r[14].toFixed(1)}</span>`:''}${r[24]!=null?`<span class="chip">Mom ${r[24]}</span>`:''}</div></div></summary>
 <div class="body"><p class="foot">${r[22]?'Fully analysed (on the shortlist).':'Preliminary buy score: price trend and news not checked (not on today\'s shortlist).'} Style: ${esc(r[23]||'–')}</p>
 <ul class="sigs">${sigs}</ul><div class="grid" style="margin-top:8px">
 <div><small>Net margin</small><b>${pct(r[8])}</b></div><div><small>3-yr CAGR</small><b class="${cls(r[6])}">${pct(r[6],1)}</b></div>
 <div><small>FCF margin</small><b>${pct(r[9])}</b></div><div><small>ROE</small><b>${pct(r[10])}</b></div>
 <div><small>Debt / equity</small><b>${r[11]==null?'–':r[11].toFixed(2)+'×'}</b></div><div><small>Shareholder yield</small><b>${pct(r[19])}</b></div></div>
 <div class="links"><a href="https://news.google.com/search?q=${encodeURIComponent(r[0]+' stock')}">News</a>
 <a href="https://www.google.com/finance/quote/${encodeURIComponent(r[0])}:${r[2]==='Nasdaq'?'NASDAQ':'NYSE'}">Chart</a>
 ${U.edit?`<a href="${U.edit}">＋ Watchlist</a>`:''}</div></div></details>`;}
function draw(){if(!U)return;const q=$('#q').value.trim().toLowerCase(),ms=num('#ms',1),mg=num('#mg',100),mb=num('#mb',1),
 sec=$('#sec').value,val=$('#val').value,reg=$('#reg').value,key=$('#sort').value;
 const i={buy:21,score:3,growth:5,cagr:6,qg:7,fcf:9,fy:15,shy:19,roe:10,mcap:18,mom:24}[key];
 const rows=U.rows.filter(r=>r[3]>=ms&&r[21]>=mb&&(r[5]??-Infinity)>=mg&&(!sec||r[13]===sec)&&(!reg||r[20]===reg)
  &&(!val||(val==='A'?r[16]==='Attractive':r[16]&&r[16]!=='Expensive'))
  &&(!q||r[0].toLowerCase().includes(q)||r[1].toLowerCase().includes(q)))
  .sort(key==='pe'?(a,b)=>(a[14]??1e9)-(b[14]??1e9):(a,b)=>(b[i]??-1e18)-(a[i]??-1e18));
 $('#cnt').textContent=`${rows.length} of ${U.rows.length} companies`+(rows.length>150?' · showing top 150':'');
 $('#list').innerHTML=rows.slice(0,150).map(row).join('')||'<p class="empty">No matches.</p>';}
['#q','#ms','#mg','#mb','#sec','#val','#reg','#sort'].forEach(id=>$(id).addEventListener('input',draw));
tab(location.hash.slice(1)||document.querySelector('.tabs button').dataset.t);
"""

STATUS_CLASS = {"Strong buy": "st-strong", "Buy": "st-buy", "Watch": "st-watch", "Avoid": "st-avoid", "Deal pending": "st-deal"}
DCA_CLASS = {"Buy extra": "strong", "DCA as usual": "healthy", "Hold": "mixed", "Wait": "mixed", "Review": "weak"}


def edit_url() -> str:
    repo = os.environ.get("GITHUB_REPOSITORY")
    return f"https://github.com/{repo}/edit/main/companies.txt" if repo else ""


def num(v, digits=1):
    return "–" if v is None or not math.isfinite(v) else f"{v:.{digits}f}"


def val_class(label):
    return "up" if label == "Attractive" else "down" if label == "Expensive" else ""


def status_badge(rk):
    return f'<span class="status {STATUS_CLASS.get(rk["status"], "")}">{rk["status"]}</span>' if rk else ""


def short_name(name: str) -> str:
    words = [w for w in name.replace(",", " ").split()
             if w.lower().strip(".") not in {"inc", "corp", "corporation", "co", "ltd", "limited", "plc", "sa", "nv", "n.v",
                                              "ag", "se", "holdings", "holding", "group", "company", "the", "/de/", "/new/",
                                              "class", "a", "adr", "s.a", "spa", "lp", "l.p"}]
    return " ".join(words[:3]) or name


def card_html(c: dict, badge: str = "", rank: int | None = None) -> str:
    e = html.escape
    a, f, m, cur = c["analysis"], c["fin"], c["analysis"]["metrics"], c["fin"]["currency"]
    val, tech, dca, rk, ev = c.get("val") or {}, c.get("tech") or {}, c.get("dca"), c.get("rank"), c.get("ev") or {}
    ring = rk["score"] if rk else a["score"]

    chips = f'<span class="chip">Health {a["score"]}</span>' if rk else ""
    chips += "".join(f'<span class="chip {sign_class(v)}">{k} {pct(v, sgn)}</span>'
                     for k, v, sgn in (("Rev", m.get("revenue_growth"), True), ("Qtr", m.get("q_growth"), True))
                     if v is not None)
    if val.get("fcf_yield") is not None:
        chips += f'<span class="chip {sign_class(val["fcf_yield"])}">FCF yld {pct(val["fcf_yield"])}</span>'
    if val.get("pe"):
        chips += f'<span class="chip">P/E {val["pe"]:.1f}</span>'
    if tech.get("ret_6m") is not None:
        chips += f'<span class="chip {sign_class(tech["ret_6m"])}">6m {pct(tech["ret_6m"], True)}</span>'
    if (ev.get("insiders") or {}).get("value", 0) >= 100_000:
        chips += '<span class="chip up">Insider buying</span>'
    if ev.get("red_flags"):
        chips += '<span class="chip down">Red flag</span>'
    news_flags = {fl for n in ev.get("news", []) for fl in n.get("flags", [])}
    if "deal" in news_flags or ev.get("takeover"):
        chips += '<span class="chip">M&amp;A news</span>'
    if "neg" in news_flags:
        chips += '<span class="chip down">Check news</span>'

    blocks = ""
    if rk:
        blocks += ('<div class="factors">' + "".join(
            f'<div><small>{k}</small><b class="{score_class(v) if v is not None else ""}">{"–" if v is None else v}</b></div>'
            for k, v in (("Quality", rk["q"]), ("Value", rk["v"]), ("Growth", rk["g"]), ("Momentum", rk["m"]))) + "</div>")
        blocks += f'<p class="foot">Style: <b>{rk["style"]}</b> · Buy score {rk["score"]}/100</p>'
        if rk["why"]:
            blocks += '<h3>Why it ranks</h3><ul class="why">' + "".join(f"<li>{e(x)}</li>" for x in rk["why"]) + "</ul>"
        if rk["risks"]:
            blocks += '<h3>Risks to check</h3><ul class="risk">' + "".join(f"<li>{e(x)}</li>" for x in rk["risks"]) + "</ul>"
    if dca and c.get("in_watchlist"):
        blocks += f'<div class="dca {DCA_CLASS.get(dca[0], "")}"><b>DCA: {dca[0]}</b><span>{e(dca[1])}</span></div>'
    if tech:
        pc = tech.get("currency", "USD")
        tgrid = [("Price", money(tech["price"], pc), None), ("Today", pct(tech.get("day_pct"), True), tech.get("day_pct")),
                 ("3 months", pct(tech.get("ret_3m"), True), tech.get("ret_3m")), ("6 months", pct(tech.get("ret_6m"), True), tech.get("ret_6m")),
                 ("12 months", pct(tech.get("ret_12m"), True), tech.get("ret_12m")), ("From 52w high", pct(tech.get("drawdown")), tech.get("drawdown")),
                 ("vs 200-day avg", pct(tech.get("vs_ma200"), True), tech.get("vs_ma200")), ("RSI (14d)", num(tech.get("rsi"), 0), None)]
        blocks += '<h3>Price &amp; trend</h3><div class="grid">' + "".join(
            f'<div><small>{k}</small><b class="{sign_class(t)}">{v}</b></div>' for k, v, t in tgrid) + "</div>"
    if val and val.get("label"):
        sp = f' <small class="inl">(sector {val["sector_pe"]:.1f})</small>' if val.get("sector_pe") else ""
        vgrid = [("Market value", money(val.get("mcap"), cur), ""), ("Valuation", val.get("label", "–"), val_class(val.get("label"))),
                 ("P/E", num(val.get("pe")) + sp, ""), ("PEG", num(val.get("peg"), 2), ""),
                 ("FCF yield", pct(val.get("fcf_yield")), sign_class(val.get("fcf_yield"))), ("Price / sales", num(val.get("ps")), ""),
                 ("Dividend yield", pct(val.get("div_yield")), ""), ("Shareholder yield", pct(val.get("shareholder_yield")), "")]
        blocks += '<h3>Valuation</h3><div class="grid">' + "".join(
            f'<div><small>{k}</small><b class="{kc}">{v}</b></div>' for k, v, kc in vgrid) + "</div>"
    elif c.get("region_src") == "EU-only":
        blocks += ('<p class="foot">No free price feed for European exchanges, so there is no valuation or price trend. '
                   'If the company also trades in the US (e.g. ASML, NVO, SAP), add the US ticker for the full analysis.</p>')

    evs = ""
    if ev.get("takeover"):
        t = ev["takeover"]
        evs += f'<li><time>{t["date"]}</time> <a href="{t["url"]}">Takeover / merger filing ({e(t["form"])})</a><span class="flag deal">M&amp;A</span></li>'
    for rf in ev.get("red_flags", []):
        evs += f'<li><time>{rf["date"]}</time> <a href="{rf["url"]}">{e(rf["text"])}</a><span class="flag neg">red flag</span></li>'
    ins = ev.get("insiders") or {}
    if ins.get("value", 0) > 0:
        evs += f'<li>Insider open-market buys last 90 days: ${ins["value"] / 1e6:.2f}M by {e(", ".join(ins["buyers"][:4]))}<span class="flag pos">insiders</span></li>'
    if ev.get("activist"):
        evs += f'<li><time>{ev["activist"]["date"]}</time> <a href="{ev["activist"]["url"]}">Large / activist stake filed ({ev["activist"]["form"]})</a></li>'
    for x in ev.get("events", [])[:5]:
        evs += f'<li><time>{x["date"]}</time> <a href="{x["url"]}">{e(x["text"])}</a></li>'
    if evs:
        blocks += f'<h3>Company events (SEC filings)</h3><ul class="news">{evs}</ul>'
    if ev.get("news"):
        items = "".join(
            f'<li><time>{n["date"]}</time> <a href="{e(n["url"])}">{e(n["title"])}</a>'
            + "".join(f'<span class="flag {fl}">{ {"deal": "M&amp;A", "neg": "negative", "pos": "positive"}[fl] }</span>' for fl in n["flags"])
            + "</li>" for n in ev["news"])
        blocks += f'<h3>Latest news</h3><ul class="news">{items}</ul>'

    sigs = "".join(
        f'<li class="{s["tone"]}"><b>{TONE_ICON[s["tone"]]} {e(s["title"])}</b><span>{e(s["detail"])}</span></li>'
        for s in a["signals"] + val.get("signals", [])) or "<li>No notable signals.</li>"
    metrics = [
        ("Revenue", money(m.get("revenue"), cur), None), ("Revenue growth", pct(m.get("revenue_growth"), True), m.get("revenue_growth")),
        ("3-yr CAGR", pct(m.get("cagr3"), True), m.get("cagr3")), ("Latest qtr YoY", pct(m.get("q_growth"), True), m.get("q_growth")),
        ("Gross margin", pct(m.get("gross_margin")), None), ("Operating margin", pct(m.get("op_margin")), m.get("op_margin")),
        ("Net margin", pct(m.get("net_margin")), m.get("net_margin")), ("Earnings growth", pct(m.get("ni_growth"), True), m.get("ni_growth")),
        ("Free cash flow", money(m.get("fcf"), cur), m.get("fcf")), ("FCF margin", pct(m.get("fcf_margin")), m.get("fcf_margin")),
        ("FCF growth", pct(m.get("fcf_growth"), True), m.get("fcf_growth")), ("Cash conversion", ratio(m.get("cash_conv")), None),
        ("ROE", pct(m.get("roe")), m.get("roe")), ("Debt / equity", ratio(m.get("de")), None),
        ("Net cash", money(m.get("net_cash"), cur), m.get("net_cash")),
        ("Share change", pct(m.get("share_change"), True), -m["share_change"] if m.get("share_change") is not None else None),
    ]
    grid = "".join(f'<div><small>{k}</small><b class="{sign_class(t)}">{v}</b></div>' for k, v, t in metrics)
    annual_chart = bar_svg([(f"FY{str(y['end'].year)[2:]}", y["revenue"], y["fcf"]) for y in f["annual"][-6:]], cur)
    quarters = [q for q in f["quarterly"][-8:] if q.get("revenue") is not None]
    q_chart = bar_svg([(f"Q{(q['end'].month - 1) // 3 + 1}'{str(q['end'].year)[2:]}", q["revenue"], None)
                       for q in quarters], cur) if len(quarters) > 1 else ""

    links = [f'<a href="{ev.get("news_link") or events_mod().news_link(short_name(c["name"]))}">News</a>']
    if c.get("exchange"):
        links.append(f'<a href="https://www.google.com/finance/quote/{e(c["ticker"])}:{"NASDAQ" if c["exchange"] == "Nasdaq" else "NYSE"}">Chart</a>')
    if c.get("cik"):
        links.append(f'<a href="https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={c["cik"]:010d}&owner=include&count=40">SEC filings</a>')
    if c.get("region_src") == "EU-only":
        links.append('<a href="https://filings.xbrl.org/">EU annual reports</a>')
    if c.get("exchange") and edit_url() and not c.get("in_watchlist"):
        links.append(f'<a href="{edit_url()}">＋ Watchlist</a>')

    sub_parts = [x for x in (c.get("region"), c.get("sector") if c.get("sector") not in (None, "Other") else None,
                             money((c.get("market") or {}).get("mcap"), "USD") if (c.get("market") or {}).get("mcap") else None) if x]
    sub = f'<small class="inl">{e(" · ".join(sub_parts))}</small>' if sub_parts else ""
    vlabel = f'<em class="{val_class(val["label"])}">{val["label"]}</em>' if val.get("label") else ""
    rank_html = f'<span class="rank">#{rank}</span>' if rank else ""
    return f"""
<details class="card" data-region="{e(c.get('region') or 'Other')}">
  <summary>{rank_html}
    <div class="ring {score_class(ring)}" style="--p:{ring}"><span>{ring}</span></div>
    <div class="who"><div><b>{e(c['ticker'])}</b> {badge} {status_badge(rk)} {vlabel} {sub}</div>
      <small>{e(c['name'])}</small><div class="chips">{chips}</div></div>
  </summary>
  <div class="body">
    {blocks}
    <h3>Business signals</h3><ul class="sigs">{sigs}</ul>
    <h3>Key metrics · latest fiscal year</h3><div class="grid">{grid}</div>
    <h3>Revenue <i class="dot rev"></i> &amp; free cash flow <i class="dot fcf"></i></h3>{annual_chart}
    {'<h3>Quarterly revenue</h3>' + q_chart if q_chart else ''}
    <div class="links">{''.join(links)}</div>
  </div>
</details>"""


def events_mod():
    import events
    return events


REGION_BAR = ('<div class="regions">' + "".join(f'<button data-r="{r}"{" class=on" if r == "All" else ""}>{r}</button>'
                                               for r in ("All", "US", "Europe", "Asia", "Other")) + "</div>")

METHOD_HTML = """
<div class="method">
<h3>How a company gets its Buy score</h3>
<p>Every day, about 2,000-4,000 companies listed in the US are scored on business quality and valuation. That includes
European and Asian companies with a US listing. The best ~200 (the shortlist) also get a price-trend check, insider and
corporate-event checks, and a news scan. Your watchlist always gets everything.</p>
<table>
<tr><td><b>Quality</b></td><td>Health score 0-100: revenue growth (1 and 3 years), margins and their trend, free cash flow, cash conversion,
return on equity, debt, net cash and share dilution.</td></tr>
<tr><td><b>Value</b></td><td>Free-cash-flow yield, P/E vs sector median, PEG (P/E ÷ growth, growth capped at 25%) and shareholder yield
(dividends + buybacks). Labelled Attractive, Fair or Expensive.</td></tr>
<tr><td><b>Growth</b></td><td>Revenue growth, with a bonus when the latest quarter is accelerating and a penalty for growth funded by cash burn.</td></tr>
<tr><td><b>Momentum</b></td><td>6- and 12-month price return, price vs 50/200-day averages, with a penalty when overheated (RSI above 78 or far above trend).</td></tr>
</table>
<h3>Two investing styles</h3>
<table>
<tr><td><b>Quality value</b></td><td>40% quality, 35% value, 10% growth, 15% momentum. Great businesses at fair prices.</td></tr>
<tr><td><b>Growth momentum</b></td><td>20% quality, 10% value, 40% growth, 30% momentum. Fast growers the market is already rewarding,
the kind of profile early winners often show.</td></tr>
</table>
<p>The Buy score is the better of the two, adjusted for:</p>
<ul class="why">
<li>Insiders buying shares on the open market with their own money (+3 to +6)</li>
<li>A large or activist stake filed (+2)</li>
</ul>
<ul class="risk">
<li>Negative headlines (-4)</li>
<li>Red flags: restatement, bankruptcy, delisting notice or auditor change cap the score at 45</li>
<li>Weak fundamentals (health below 35) cap the score at 60</li>
<li>A pending takeover gets the status "Deal pending", because the price is tied to the deal</li>
</ul>
<h3>Statuses</h3>
<table>
<tr><td><span class="status st-strong">Strong buy</span></td><td>78+: top on several factors at once</td></tr>
<tr><td><span class="status st-buy">Buy</span></td><td>68-77</td></tr>
<tr><td><span class="status st-watch">Watch</span></td><td>55-67: interesting, but something is missing (price, trend or quality)</td></tr>
<tr><td><span class="status st-avoid">Avoid</span></td><td>below 55, or red flags</td></tr>
</table>
<h3>DCA actions (watchlist)</h3>
<p><b>Buy extra</b>: quality 70+, not expensive, and temporarily down (15%+ below the high, under the 200-day average, or oversold).
<b>DCA as usual</b>: healthy and fairly priced. <b>Hold</b>: average quality or a high price. <b>Wait</b>: expensive and stretched.
<b>Review</b>: weak fundamentals.</p>
<h3>Data and limits</h3>
<p>Financials: SEC EDGAR (10-K, 10-Q, 20-F, 40-F), plus EU annual reports (ESEF) for European watchlist stocks.
Prices: Nasdaq end-of-day and current price. FX: ECB. Events: SEC 8-K/6-K, Form 4, SC 13D, SC TO and merger proxies. News: Google News.
Stocks listed only in Europe or Asia (no US listing) have no free price feed: they get a quality score only.
Momentum for companies outside the shortlist builds up automatically from the app's own daily price history.
These are rule-based signals to find companies worth researching. They are not investment advice: always read the latest results and news before you buy.</p>
</div>"""


def list_section(items: list[dict], intro: str, cap: int) -> str:
    if not items:
        return '<p class="empty">Nothing here yet. The full analysis appears after the first run.</p>'
    return (f'<p class="desc">{intro}</p>{REGION_BAR}' +
            "".join(card_html(c, rank=i + 1) for i, c in enumerate(items[:cap])))


def render(ctx: dict) -> str:
    e = html.escape
    cfg = ctx["cfg"]
    ranked = ctx["ranked"]
    top = [c for c in ranked if c["rank"]["status"] in ("Strong buy", "Buy", "Watch")]
    growth = sorted([c for c in ranked if c["rank"]["style"] == "Growth momentum" and c["rank"]["status"] != "Deal pending"],
                    key=lambda c: -c["rank"]["score"])
    value = sorted([c for c in ranked if c["rank"]["style"] == "Quality value" and c["rank"]["status"] != "Deal pending"],
                   key=lambda c: -c["rank"]["score"])

    tabs = {}
    tabs["top"] = ("🏆 Top", list_section(
        top, "Best overall Buy scores today across all factors (quality, value, growth, momentum, events). "
             "Tap a company for the reasons, risks and news.", cfg.get("top_count", 50)))
    tabs["growth"] = ("🚀 Growth", list_section(
        growth, "Growth momentum: fast-growing companies whose price trend confirms the story. Higher potential, higher risk.", 30))
    tabs["value"] = ("💎 Value", list_section(
        value, "Quality value: strong businesses at reasonable prices. The steadiest compounders.", 30))

    watch = ctx["watch"]
    order = ["Buy extra", "DCA as usual", "Hold", "Wait", "Review", None]
    ws = sorted(watch, key=lambda c: (order.index(c["dca"][0] if c.get("dca") else None),
                                      -(c["rank"]["score"] if c.get("rank") else 0)))
    rows = ""
    for c in ws:
        label = (c.get("val") or {}).get("label") or "no price data"
        dca = f'<span class="badge">DCA: {c["dca"][0]}</span>' if c.get("dca") else ""
        rows += f'<div><b>{e(c["ticker"])}</b>{status_badge(c.get("rank"))}<span class="grow">{label}</span>{dca}</div>' 
    wh = (f'<p class="desc">Your companies: Buy-score status, valuation and suggested DCA action.</p><div class="overview">{rows}</div>'
          + "".join(card_html(c) for c in ws)) if ws else '<p class="empty">Add tickers to companies.txt.</p>'
    if edit_url():
        wh += f'<div class="links"><a href="{edit_url()}">Edit watchlist</a></div>'
    tabs["watch"] = ("⭐ Watchlist", wh)

    new_parts = []
    if ctx["new_picks"]:
        new_parts.append('<h2>New buy signals</h2><p class="desc">Became Strong buy or Buy since the last check.</p>')
        new_parts += [card_html(c, '<span class="badge">NEW</span>') for c in ctx["new_picks"][:20]]
    disc = ctx["disc"]
    for key, title, desc in (("rising", "Biggest improvers", "Health score up 10+ points since the last check."),
                             ("accel", "Growth accelerating", "Latest quarter grew faster than the one before. Often an early sign."),
                             ("turn", "Turnarounds", "Turned profitable this year, with revenue growing."),
                             ("fast", "Fast growers", "Revenue +25% or more, profitable and cash-generative.")):
        items = disc.get(key) or []
        if items:
            new_parts.append(f'<h2>{title} · {len(items)}</h2><p class="desc">{desc}</p>')
            new_parts += [card_html(c) for c in items[:12]]
    tabs["new"] = ("🆕 New", "".join(new_parts) or '<p class="empty">New signals appear from the second run onward.</p>')

    tabs["all"] = ("🔎 Explore", """
<div class="filters">
  <div class="full"><input id="q" placeholder="Search ticker or name" autocomplete="off"></div>
  <div><label>Region</label><select id="reg"><option value="">All</option><option>US</option><option>Europe</option><option>Asia</option><option>Other</option></select></div>
  <div><label>Sector</label><select id="sec"></select></div>
  <div><label>Valuation</label><select id="val"><option value="">Any</option><option value="A">Attractive</option><option value="F">Fair or attractive</option></select></div>
  <div><label>Min buy score</label><input id="mb" type="number" inputmode="numeric" placeholder="any"></div>
  <div><label>Min health score</label><input id="ms" type="number" inputmode="numeric" placeholder="any"></div>
  <div><label>Min revenue growth %</label><input id="mg" type="number" inputmode="numeric" placeholder="any"></div>
  <div class="full"><label>Sort by</label><select id="sort"><option value="buy">Buy score</option><option value="score">Health score</option>
  <option value="growth">Revenue growth</option><option value="qg">Latest quarter growth</option><option value="mom">Momentum</option>
  <option value="fy">FCF yield</option><option value="pe">Lowest P/E</option><option value="cagr">3-yr CAGR</option>
  <option value="fcf">FCF margin</option><option value="shy">Shareholder yield</option><option value="roe">Return on equity</option>
  <option value="mcap">Market value</option></select></div>
</div><p class="desc" id="cnt"></p><div id="list"></div>""")

    alert_items = "".join(
        f'<li class="{x["tone"]}"><b>{TONE_ICON.get(x["tone"], "•")} {e(x["ticker"])} · {e(x["title"])}</b>'
        f'<span>{e(x["body"])}</span><time>{x["date"][:16].replace("T", " ")} UTC</time></li>' for x in ctx["alerts"][:120])
    ah = f'<ul class="alerts">{alert_items}</ul>' if alert_items else '<p class="empty">No alerts yet.</p>'
    if ctx["errors"]:
        ah += '<h2>Problems in last run</h2><ul class="err">' + "".join(f"<li>{e(x)}</li>" for x in ctx["errors"][:40]) + "</ul>"
    tabs["alerts"] = ("🔔 Alerts", ah)
    tabs["method"] = ("ℹ️ Method", METHOD_HTML)

    nav = "".join(f'<button data-t="{k}">{v[0]}</button>' for k, v in tabs.items())
    sections = "".join(f'<section id="{k}" hidden>{v[1]}</section>' for k, v in tabs.items())
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-title" content="Pulse">
<meta name="theme-color" content="#0b0f14">
<title>CompanyPulse</title><style>{CSS}{EXTRA_CSS}</style></head><body>
<header><h1>CompanyPulse</h1><small>Updated {ctx['generated']} · {ctx['universe_n']} companies scored · {len(ranked)} fully analysed · not investment advice</small></header>
<nav class="tabs">{nav}</nav>
<main>{sections}</main>
<script>{JS}</script>
</body></html>"""


def universe_json(universe: list[dict]) -> str:
    sig: dict[str, list[str]] = {}
    rows = []

    def r4(v):
        return None if v is None or not math.isfinite(v) else round(v, 4)

    for c in universe:
        m, val, mk, rk = c["analysis"]["metrics"], c.get("val") or {}, c.get("market") or {}, c.get("rank")
        all_sigs = c["analysis"]["signals"] + val.get("signals", [])
        for s in all_sigs:
            sig.setdefault(s["id"], [s["tone"], "Multi-year growth streak" if s["id"] == "rev_streak" else s["title"]])
        pre = c.get("pre_rank") or {}
        rows.append([c["ticker"], c["name"], c["exchange"], c["analysis"]["score"], m.get("revenue"),
                     r4(m.get("revenue_growth")), r4(m.get("cagr3")), r4(m.get("q_growth")), r4(m.get("net_margin")),
                     r4(m.get("fcf_margin")), r4(m.get("roe")), r4(m.get("de")), [s["id"] for s in all_sigs],
                     c.get("sector") or "Other", r4(val.get("pe")), r4(val.get("fcf_yield")), val.get("label"),
                     r4(mk.get("day_pct")), mk.get("mcap"), r4(val.get("shareholder_yield")), c.get("region") or "Other",
                     (rk or pre).get("score", 0), rk["status"] if rk else "", (rk or pre).get("style", ""),
                     (rk or pre).get("m")])
    return json.dumps({"rows": rows, "sig": sig, "edit": edit_url()}, separators=(",", ":"))

# --------------------------------------------------------------------------- orchestration

def load_state() -> dict:
    src = STATE_FILE if STATE_FILE.exists() else LEGACY_STATE if LEGACY_STATE.exists() else None
    state = json.loads(src.read_text(encoding="utf-8")) if src else {}
    for k, default in (("companies", {}), ("alerts", []), ("flags", {}), ("lei", {}), ("picks", {})):
        state.setdefault(k, default)
    return state


def watchlist(cfg, state, tickers, screener, medians, universe_by_ticker):
    import europe
    import market

    tmap = ticker_map()
    companies, errors = [], []
    for t, hint in tickers:
        print(f"watchlist {t} ...", flush=True)
        if t in universe_by_ticker:            # already analysed by discovery: reuse
            c = universe_by_ticker[t]
            c["in_watchlist"] = True
            companies.append(c)
            continue
        c = {"ticker": t, "in_watchlist": True}
        try:
            if europe.is_eu(t):
                cached = state["lei"].get(t)
                lei = cached["lei"] if cached and cached.get("hint") == hint else europe.resolve_lei(hint)
                if not lei:
                    raise ValueError("add the company name or LEI after '=', e.g. `ABI.BR = Anheuser-Busch`")
                state["lei"][t] = {"hint": hint, "lei": lei}
                fin = europe.fetch(lei)
                c.update(name=hint or t, cik=None, fin=fin, region="Europe", region_src="EU-only")
            else:
                info = tmap.get(t) or tmap.get(t.replace(".", "-"))
                if not info:
                    raise ValueError("ticker not found on SEC EDGAR. For European stocks add the exchange suffix, e.g. ABI.BR")
                import discover
                fin = discover.company_fin(info["cik"])
                mk = screener.get(market.norm(t)) or {}
                c.update(name=info["name"], cik=info["cik"], fin=fin, market=mk,
                         exchange=discover.LISTING.get(info["cik"], {}).get("exchange", "NYSE"),
                         sector=mk.get("sector"), region=market.region(mk.get("country")))
            if not fin["annual"]:
                raise ValueError("no annual financials found")
        except Exception as ex:
            errors.append(f"{t}: {ex}")
            continue
        c["analysis"] = analyze(c["fin"], cfg)
        if c.get("market", {}).get("mcap"):
            mcap = c["market"]["mcap"]
            if c["fin"]["currency"] != "USD":
                rate = market.fx("USD", c["fin"]["currency"])
                mcap = mcap * rate if rate else None
            c["val"] = market.valuation(c["fin"], c["analysis"]["metrics"], mcap, medians.get(c.get("sector")), c.get("sector"))
        companies.append(c)
    return companies, errors


def enrich(cands, screener, series, cfg, errors, deep: bool):
    """Price trend for candidates; SEC events + insiders + news when deep=True."""
    import cache
    import events
    import market
    import ranking
    for c in cands:
        t = c["ticker"]
        mk = screener.get(market.norm(t)) or {}
        if not c.get("tech") and c.get("cik") and mk:
            try:
                hist = cache.get(f"hist:{t}", 20, lambda: market.nasdaq_history(t))
                c["tech"] = market.technicals(hist, mk.get("price"), mk.get("day_pct"))
                time.sleep(0.15)
            except Exception:
                own = series.get(market.norm(t))
                if own and len(own) >= 130:
                    c["tech"] = market.technicals({"closes": own, "price": own[-1]}, mk.get("price"), mk.get("day_pct"))
        if deep and "ev" not in c:
            c["ev"] = events.enrich(c.get("cik"), short_name(c["name"]) if c.get("region_src") != "EU-only" else c["name"])
        c["rank"] = ranking.score(c)
        if c.get("val") and c.get("tech"):
            c["dca"] = market.dca_action(c["analysis"]["score"], c["val"], c["tech"])


def main():
    import cache
    import market
    import ranking

    cfg = load_config()
    tickers = load_tickers()
    SITE.mkdir(exist_ok=True)
    STATE_FILE.parent.mkdir(exist_ok=True)
    state = load_state()
    errors: list[str] = []
    now = datetime.now(timezone.utc).isoformat(timespec="minutes")
    today = datetime.now(timezone.utc).date()

    try:
        screener = market.nasdaq_screener()
    except Exception as ex:
        screener = {}
        errors.append(f"Nasdaq price data unavailable this run ({ex}); valuations skipped.")
    series = market.update_price_store(screener, cache.DIR / "prices.json") if screener else {}

    # 1. whole-market screen (fundamentals + valuation)
    universe, disc, medians = [], {}, {}
    if cfg.get("discovery", True):
        try:
            import discover
            universe = discover.run(cfg, screener)
            medians = discover.attach_market(universe, screener) if screener else {}
            disc = discover.lists(universe, cfg)
        except Exception as ex:
            errors.append(f"Discovery failed: {ex}")

    # momentum from the app's own price history for everyone (once ~6 months of history exist)
    for c in universe:
        own = series.get(market.norm(c["ticker"]))
        mk = c.get("market") or {}
        if own and len(own) >= 130:
            c["tech"] = market.technicals({"closes": own, "price": own[-1]}, mk.get("price"), mk.get("day_pct"))
        qv, gm = ranking.prelim(c)
        mo = ranking.momentum_score(c.get("tech") or {})
        if mo is not None:
            qv, gm = qv - 7.5 + 0.15 * mo, gm - 15 + 0.30 * mo
        c["pre_rank"] = {"score": round(max(qv, gm)), "style": "Quality value" if qv >= gm else "Growth momentum", "m": mo}

    # 2. shortlist: best preliminary scores in both styles (only investable sizes)
    min_mcap = cfg.get("rank_min_mcap", 2e8)
    investable = [c for c in universe if ((c.get("market") or {}).get("mcap") or 0) >= min_mcap and c["analysis"]["score"] >= 30]
    n = cfg.get("shortlist_size", 120)
    by_qv = sorted(investable, key=lambda c: -ranking.prelim(c)[0])[:n]
    by_gm = sorted(investable, key=lambda c: -ranking.prelim(c)[1])[:n]
    # always check the fastest growers too: early winners often look expensive and are not yet profitable
    by_growth = sorted(investable, key=lambda c: (-ranking.growth_score(c["analysis"]["metrics"]),
                                                  -(c["analysis"]["metrics"].get("revenue_growth") or 0)))[:cfg.get("growth_slice", 60)]
    shortlist = list({c["ticker"]: c for c in by_qv + by_gm + by_growth}.values())
    print(f"Shortlist: {len(shortlist)} companies", flush=True)

    # 3. watchlist (reuses discovery data where possible)
    by_ticker = {c["ticker"]: c for c in universe}
    watch, w_err = watchlist(cfg, state, tickers, screener, medians, by_ticker)
    errors += w_err

    # 4. price trend + events for shortlist, then news for the best ones and the watchlist
    cands = list({id(c): c for c in shortlist + watch}.values())
    enrich(cands, screener, series, cfg, errors, deep=False)            # price trend
    ranking.add_relative_strength(list({id(t): t for t in (c.get("tech") for c in universe + cands) if t}.values()))
    for c in cands:
        c["rank"] = ranking.score(c)
    shortlist.sort(key=lambda c: -c["rank"]["score"])
    deep = shortlist[:cfg.get("deep_count", 70)]
    enrich(list({id(c): c for c in deep + watch}.values()), screener, series, cfg, errors, deep=True)  # events + news
    ranking.assign_statuses(cands, cfg)
    ranked = sorted(shortlist, key=lambda c: (ranking.STATUS_ORDER.index(c["rank"]["status"]), -c["rank"]["score"]))

    # 5. alerts
    alerts = []

    def add_alert(t, tone, title, body):
        alerts.append({"date": now, "ticker": t, "tone": tone, "title": title, "body": body})

    first_run = not state.get("picks_initialized")
    new_picks, new_t = [], set()
    for c in ranked:
        if c["rank"]["status"] in ("Strong buy", "Buy"):
            seen = state["picks"].get(c["ticker"])
            if not first_run and (not seen or (today - date.fromisoformat(seen)).days > 14):
                new_picks.append(c)
                new_t.add(c["ticker"])
            state["picks"][c["ticker"]] = today.isoformat() if (not seen or c["ticker"] in new_t) else seen
    state["picks"] = {t: d for t, d in state["picks"].items() if (today - date.fromisoformat(d)).days <= 60}
    state["picks_initialized"] = True
    for c in new_picks[:20]:
        add_alert(c["ticker"], "pos", f"New {c['rank']['status']} signal ({c['rank']['score']})",
                  f"{c['rank']['style']} · " + "; ".join(c["rank"]["why"][:2]))

    for c in watch:
        t = c["ticker"]
        for a in detect_changes(t, state["companies"].get(t), c["analysis"], c["fin"], cfg):
            alerts.append(a)
        state["companies"][t] = {"score": c["analysis"]["score"], "signals": [s["id"] for s in c["analysis"]["signals"]],
                                 "filing": c["fin"]["filing"]["accn"] if c["fin"].get("filing") else None}
        flags = market.price_flags(c["tech"], c["dca"][0] if c.get("dca") else "", today.isoformat()) if c.get("tech") else {}
        ev = c.get("ev") or {}
        if ev.get("takeover"):
            flags["ev_takeover"] = ("warn", "Takeover / merger filing", f"{ev['takeover']['form']} filed {ev['takeover']['date']}. Read the details.")
        for rf in ev.get("red_flags", []):
            flags[f"ev_rf_{rf['date']}"] = ("neg", f"Red flag: {rf['text']}", f"Filed {rf['date']}. Check the filing and news.")
        if (ev.get("insiders") or {}).get("value", 0) >= 100_000:
            flags["ev_insider"] = ("pos", "Insiders are buying", f"${ev['insiders']['value'] / 1e6:.2f}M bought on the open market in 90 days.")
        if c.get("rank") and c["rank"]["status"] in ("Strong buy", "Buy"):
            flags[f"rank_{c['rank']['status']}"] = ("pos", f"Now rated {c['rank']['status']}", "; ".join(c["rank"]["why"][:2]))
        prev = state["flags"].get(t)
        if prev is not None:
            for k, (tone, title, body) in flags.items():
                if k not in prev:
                    add_alert(t, tone, title, body)
        state["flags"][t] = list(flags)
    keep = {t for t, _ in tickers}
    for k in ("companies", "flags", "lei"):
        state[k] = {t: v for t, v in state[k].items() if t in keep}

    # discovery bookkeeping (improvers)
    prev_disc = state.get("discovery") or {}
    prev_scores = prev_disc.get("scores", {})
    rising = []
    for c in universe:
        ps = prev_scores.get(c["ticker"])
        if ps is not None and c["analysis"]["score"] - ps >= 10:
            c["prev_score"] = ps
            rising.append(c)
    disc["rising"] = sorted(rising, key=lambda c: -(c["analysis"]["score"] - c["prev_score"]))
    if universe:
        state["discovery"] = {"scores": {c["ticker"]: c["analysis"]["score"] for c in universe}}

    state["alerts"] = (list(reversed(alerts)) + state["alerts"])[:300]
    state["last_run"] = now

    ctx = {"cfg": cfg, "ranked": ranked, "watch": watch, "new_picks": new_picks, "disc": disc, "alerts": state["alerts"],
           "errors": errors, "universe_n": len(universe), "generated": datetime.now(timezone.utc).strftime("%d %b %Y %H:%M UTC")}
    (SITE / "index.html").write_text(render(ctx), encoding="utf-8")
    (SITE / "universe.json").write_text(universe_json(universe), encoding="utf-8")

    # 6. notifications
    if new_picks and cfg.get("notify_buy_signals", True):
        lines = [f"{c['ticker']} {c['rank']['score']} {c['rank']['status']} · {c['rank']['style']} · {c['name'][:22]}" for c in new_picks[:10]]
        push(f"🟢 {len(new_picks)} new buy signal{'s' if len(new_picks) > 1 else ''}", "\n".join(lines), ["green_circle"], priority=4)
    wl_alerts: dict[str, list[dict]] = {}
    for a in alerts:
        if a["ticker"] in keep:
            wl_alerts.setdefault(a["ticker"], []).append(a)
    for t, items in wl_alerts.items():
        c = next((c for c in watch if c["ticker"] == t), None)
        head = t + (f" · {c['rank']['status']} {c['rank']['score']}" if c and c.get("rank") else "") + \
            (f" · DCA: {c['dca'][0]}" if c and c.get("dca") else "")
        neg = any(i["tone"] in ("neg", "warn") for i in items)
        push(head, "\n".join(f"{TONE_ICON.get(i['tone'], '•')} {i['title']}: {i['body']}" for i in items),
             ["warning"] if neg else ["chart_with_upwards_trend"], priority=4 if neg else 3)
    if cfg["daily_summary"] and state.get("summary_date") != today.isoformat() and (watch or ranked):
        state["summary_date"] = today.isoformat()
        lines = ["Top picks: " + ", ".join(f"{c['ticker']} {c['rank']['score']}" for c in ranked[:6])]
        lines += [f"{c['ticker']}: {c['rank']['status'] if c.get('rank') else '–'}" + (f" · DCA {c['dca'][0]}" if c.get("dca") else "")
                  for c in watch[:10]]
        push("CompanyPulse · daily summary", "\n".join(lines) + ("\n⚠ " + "; ".join(errors[:2]) if errors else ""),
             ["newspaper"], priority=2)

    STATE_FILE.write_text(json.dumps(state, separators=(",", ":")), encoding="utf-8")
    cache.prune()
    print(f"\nDone: {len(universe)} scored, {len(shortlist)} shortlisted, "
          f"{sum(c['rank']['status'] == 'Strong buy' for c in ranked)} strong buy, {sum(c['rank']['status'] == 'Buy' for c in ranked)} buy, "
          f"{len(new_picks)} new picks, {len(watch)} watchlist, {len(alerts)} alerts, {len(errors)} errors.")
    for x in errors[:20]:
        print("  !", x)


if __name__ == "__main__":
    main()
