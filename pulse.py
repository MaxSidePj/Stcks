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
DOCS = ROOT / "docs"
STATE_FILE = DOCS / "state.json"

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


def load_tickers() -> list[str]:
    tickers = []
    for line in (ROOT / "companies.txt").read_text(encoding="utf-8").splitlines():
        t = line.split("#", 1)[0].strip().upper()
        if t and t not in tickers:
            tickers.append(t)
    return tickers

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
    units = set()
    for tax, name in REVENUE + NETINC:
        units |= set(facts.get(tax, {}).get(name, {}).get("units", {}).keys())
    cur = "USD" if "USD" in units or not units else next((u for u in sorted(units) if len(u) == 3 and u.isupper()), "USD")

    revA, niA = series(facts, REVENUE, cur, "annual"), series(facts, NETINC, cur, "annual")
    gpA, opA = series(facts, GROSS, cur, "annual"), series(facts, OPINC, cur, "annual")
    shA = series(facts, SHARES, "shares", "annual")
    ocfA, capA = series(facts, OCF, cur, "annual"), series(facts, CAPEX, cur, "annual")
    cashI, debtI, eqI = (series(facts, x, cur, "instant") for x in (CASH, DEBT, EQUITY))

    annual = []
    for end in sorted(revA or niA)[-10:]:
        src = revA.get(end) or niA.get(end)
        p = {"start": src["start"], "end": end,
             "revenue": lookup(revA, end), "net_income": lookup(niA, end),
             "gross_profit": lookup(gpA, end), "operating_income": lookup(opA, end),
             "shares": lookup(shA, end), "ocf": lookup(ocfA, end), "capex": lookup(capA, end),
             "cash": lookup(cashI, end), "debt": lookup(debtI, end), "equity": lookup(eqI, end)}
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

JS = r"""
const $=s=>document.querySelector(s);
function tab(t){document.querySelectorAll('.tabs button').forEach(b=>b.classList.toggle('on',b.dataset.t===t));
 document.querySelectorAll('main>section').forEach(s=>s.hidden=s.id!==t);history.replaceState(null,'','#'+t);
 if(t==='all')loadAll();}
document.querySelectorAll('.tabs button').forEach(b=>b.onclick=()=>tab(b.dataset.t));
let U=null;
async function loadAll(){if(U)return;$('#list').innerHTML='<p class="empty">Loading…</p>';
 U=await (await fetch('universe.json')).json();draw();}
const esc=s=>String(s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const pct=(v,s)=>v==null?'–':(s&&v>0?'+':'')+(v*100).toFixed(1)+'%';
const cls=v=>v==null?'':v>0.005?'up':v<-0.005?'down':'';
const sc=s=>s>=70?'strong':s>=55?'healthy':s>=40?'mixed':'weak';
function money(v){if(v==null)return'–';const a=Math.abs(v),g=v<0?'-$':'$';
 return a>=1e12?g+(a/1e12).toFixed(2)+'T':a>=1e9?g+(a/1e9).toFixed(2)+'B':g+(a/1e6).toFixed(0)+'M';}
function num(id,div){const v=$(id).value;return v===''?-Infinity:+v/div;}
function row(r){
 const sigs=r[12].map(i=>`<li class="${U.sig[i][0]}"><b>${esc(U.sig[i][1])}</b></li>`).join('')||'<li>No notable signals.</li>';
 return `<details class="card"><summary><div class="ring ${sc(r[3])}" style="--p:${r[3]}"><span>${r[3]}</span></div>
 <div class="who"><div><b>${esc(r[0])}</b> <small class="inl">${r[2]} · ${money(r[4])} revenue</small></div><small>${esc(r[1])}</small>
 <div class="chips"><span class="chip ${cls(r[5])}">Rev ${pct(r[5],1)}</span><span class="chip ${cls(r[9])}">FCF ${pct(r[9])}</span>
 <span class="chip ${cls(r[8])}">Net ${pct(r[8])}</span>${r[7]!=null?`<span class="chip ${cls(r[7])}">Qtr ${pct(r[7],1)}</span>`:''}</div></div></summary>
 <div class="body"><ul class="sigs">${sigs}</ul><div class="grid" style="margin-top:8px">
 <div><small>3-yr CAGR</small><b class="${cls(r[6])}">${pct(r[6],1)}</b></div><div><small>ROE</small><b>${pct(r[10])}</b></div>
 <div><small>Debt / equity</small><b>${r[11]==null?'–':r[11].toFixed(2)+'×'}</b></div><div><small>FCF margin</small><b>${pct(r[9])}</b></div></div>
 ${U.edit?`<p class="foot"><a href="${U.edit}">＋ Add to watchlist</a> (adds a full deep-dive)</p>`:''}</div></details>`;}
function draw(){if(!U)return;const q=$('#q').value.trim().toLowerCase(),ms=num('#ms',1),mg=num('#mg',100),mf=num('#mf',100);
 const i={score:3,growth:5,cagr:6,qg:7,fcf:9,roe:10,rev:4}[$('#sort').value];
 const rows=U.rows.filter(r=>r[3]>=ms&&(r[5]??-Infinity)>=mg&&(r[9]??-Infinity)>=mf&&(!q||r[0].toLowerCase().includes(q)||r[1].toLowerCase().includes(q)))
  .sort((a,b)=>(b[i]??-1e18)-(a[i]??-1e18));
 $('#cnt').textContent=`${rows.length} of ${U.rows.length} companies`+(rows.length>150?' · showing top 150':'');
 $('#list').innerHTML=rows.slice(0,150).map(row).join('')||'<p class="empty">No matches.</p>';}
['#q','#ms','#mg','#mf','#sort'].forEach(id=>$(id).addEventListener('input',draw));
tab(location.hash.slice(1)||document.querySelector('.tabs button').dataset.t);
"""

DISCOVERY_SECTIONS = [
    ("rising", "Biggest improvers", "Health score up 10+ points since the last check.", 25),
    ("radar", "Top ranked", "Highest health scores across the market.", 40),
    ("fast", "Fast growers", "Revenue +25% or more, profitable, and free cash flow positive.", 25),
    ("accel", "Growth accelerating", "Latest quarter grew faster than the quarter before.", 25),
    ("cash", "Cash machines", "Free cash flow of 25%+ of revenue and still growing.", 25),
    ("turn", "Turnarounds", "Turned profitable this year, with revenue growing.", 20),
]


def edit_url() -> str:
    repo = os.environ.get("GITHUB_REPOSITORY")
    return f"https://github.com/{repo}/edit/main/companies.txt" if repo else ""


def card_html(c: dict, badge: str = "") -> str:
    e = html.escape
    a, f, m, cur = c["analysis"], c["fin"], c["analysis"]["metrics"], c["fin"]["currency"]
    chips = "".join(
        f'<span class="chip {sign_class(v)}">{k} {pct(v, sgn)}</span>'
        for k, v, sgn in (("Rev", m.get("revenue_growth"), True), ("FCF", m.get("fcf_margin"), False),
                          ("Net", m.get("net_margin"), False)))
    if m.get("q_growth") is not None:
        chips += f'<span class="chip {sign_class(m["q_growth"])}">Qtr {pct(m["q_growth"], True)}</span>'
    sigs = "".join(
        f'<li class="{s["tone"]}"><b>{TONE_ICON[s["tone"]]} {e(s["title"])}</b><span>{e(s["detail"])}</span></li>'
        for s in a["signals"]) or "<li>No notable signals.</li>"
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
    filing = f.get("filing")
    foot = [f'Latest filing: {filing["form"]} filed {filing["filed"]}'] if filing else []
    foot.append(f'<a href="https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={c["cik"]:010d}&type=10-&owner=include&count=40">SEC filings</a>')
    if c.get("exchange") and edit_url():
        foot.append(f'<a href="{edit_url()}">＋ Add to watchlist</a>')
    sub = f'<small class="inl">{c["exchange"]} · {money(m.get("revenue"), cur)} revenue</small>' if c.get("exchange") else ""
    return f"""
<details class="card">
  <summary>
    <div class="ring {score_class(a['score'])}" style="--p:{a['score']}"><span>{a['score']}</span></div>
    <div class="who"><div><b>{e(c['ticker'])}</b> {badge} <em class="{score_class(a['score'])}">{a['rating']}</em> {sub}</div>
      <small>{e(c['name'])}</small><div class="chips">{chips}</div></div>
    {spark_svg([y['revenue'] for y in f['annual'][-5:]])}
  </summary>
  <div class="body">
    <h3>Signals</h3><ul class="sigs">{sigs}</ul>
    <h3>Key metrics · latest fiscal year</h3><div class="grid">{grid}</div>
    <h3>Revenue <i class="dot rev"></i> &amp; free cash flow <i class="dot fcf"></i></h3>{annual_chart}
    {'<h3>Quarterly revenue</h3>' + q_chart if q_chart else ''}
    <p class="foot">{' · '.join(foot)}</p>
  </div>
</details>"""


def render(watch: list[dict], disc: dict, new_radar: list[dict], universe_n: int,
           alerts: list[dict], errors: list[str], generated: str, cfg: dict) -> str:
    e = html.escape

    # --- Discover tab
    parts = []
    if new_radar:
        parts.append('<h2>New on your radar</h2><p class="desc">Crossed a health score of '
                     f'{cfg.get("discovery_min_score", 75)} since the last check.</p>')
        parts += [card_html(c, '<span class="badge">NEW</span>') for c in new_radar[:30]]
    for key, title, desc, cap in DISCOVERY_SECTIONS:
        items = disc.get(key) or []
        if not items:
            continue
        parts.append(f'<h2>{title} · {len(items)}</h2><p class="desc">{desc}'
                     f'{f" Showing top {cap}; see Explore all for the rest." if len(items) > cap else ""}</p>')
        for c in items[:cap]:
            badge = f'<span class="badge">+{c["analysis"]["score"] - c["prev_score"]}</span>' if key == "rising" else ""
            parts.append(card_html(c, badge))
    discover_html = "".join(parts) or '<p class="empty">Discovery data not available yet.</p>'

    # --- Watchlist tab
    watch_html = "".join(card_html(c) for c in sorted(watch, key=lambda c: -c["analysis"]["score"])) \
        or '<p class="empty">Your watchlist is empty. Add tickers to companies.txt.</p>'
    if edit_url():
        watch_html += f'<p class="foot"><a href="{edit_url()}">Edit watchlist (companies.txt)</a></p>'

    # --- Alerts tab
    alert_items = "".join(
        f'<li class="{x["tone"]}"><b>{TONE_ICON.get(x["tone"], "•")} {e(x["ticker"])} · {e(x["title"])}</b>'
        f'<span>{e(x["body"])}</span><time>{x["date"][:10]}</time></li>' for x in alerts[:80])
    alerts_html = f'<ul class="alerts">{alert_items}</ul>' if alert_items else \
        '<p class="empty">No alerts yet. They appear from the second daily run onward.</p>'
    if errors:
        alerts_html += '<h2>Problems in last run</h2><ul class="err">' + "".join(f"<li>{e(x)}</li>" for x in errors) + "</ul>"

    explore_html = """
<div class="filters">
  <div class="full"><input id="q" placeholder="Search ticker or name" autocomplete="off"></div>
  <div><label>Min score</label><input id="ms" type="number" inputmode="numeric" placeholder="any"></div>
  <div><label>Min revenue growth %</label><input id="mg" type="number" inputmode="numeric" placeholder="any"></div>
  <div><label>Min FCF margin %</label><input id="mf" type="number" inputmode="numeric" placeholder="any"></div>
  <div><label>Sort by</label><select id="sort"><option value="score">Health score</option><option value="growth">Revenue growth</option>
  <option value="qg">Latest quarter growth</option><option value="cagr">3-yr CAGR</option><option value="fcf">FCF margin</option>
  <option value="roe">Return on equity</option><option value="rev">Revenue size</option></select></div>
</div><p class="desc" id="cnt"></p><div id="list"></div>"""

    first = "disc" if discover_html and disc else "watch"
    order = [("disc", "Discover"), ("watch", f"Watchlist"), ("all", "Explore all"), ("alerts", "Alerts")]
    if first == "watch":
        order = [order[1], order[0], order[2], order[3]]
    tabs = "".join(f'<button data-t="{k}">{v}</button>' for k, v in order)

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-title" content="Pulse">
<meta name="theme-color" content="#0b0f14">
<title>CompanyPulse</title><style>{CSS}</style></head><body>
<header><h1>CompanyPulse</h1><small>Updated {generated} · {universe_n} companies screened · data: SEC EDGAR · not investment advice</small></header>
<nav class="tabs">{tabs}</nav>
<main>
<section id="disc">{discover_html}</section>
<section id="watch" hidden>{watch_html}</section>
<section id="all" hidden>{explore_html}</section>
<section id="alerts" hidden>{alerts_html}</section>
</main>
<script>{JS}</script>
</body></html>"""


def universe_json(universe: list[dict]) -> str:
    sig: dict[str, list[str]] = {}
    rows = []

    def r4(v):
        return None if v is None or not math.isfinite(v) else round(v, 4)

    for c in universe:
        m = c["analysis"]["metrics"]
        for s in c["analysis"]["signals"]:
            sig.setdefault(s["id"], [s["tone"], "Multi-year growth streak" if s["id"] == "rev_streak" else s["title"]])
        rows.append([c["ticker"], c["name"], c["exchange"], c["analysis"]["score"], m.get("revenue"),
                     r4(m.get("revenue_growth")), r4(m.get("cagr3")), r4(m.get("q_growth")), r4(m.get("net_margin")),
                     r4(m.get("fcf_margin")), r4(m.get("roe")), r4(m.get("de")),
                     [s["id"] for s in c["analysis"]["signals"]]])
    return json.dumps({"rows": rows, "sig": sig, "edit": edit_url()}, separators=(",", ":"))

# --------------------------------------------------------------------------- main

def run_watchlist(cfg: dict, state: dict, tickers: list[str]):
    tmap = ticker_map()
    companies, new_alerts, errors = [], [], []
    for t in tickers:
        info = tmap.get(t) or tmap.get(t.replace(".", "-"))
        if not info:
            errors.append(f"{t}: ticker not found on SEC EDGAR (only US-listed companies are covered).")
            continue
        print(f"{t} ...", flush=True)
        try:
            raw = json.loads(sec_get(f"https://data.sec.gov/api/xbrl/companyfacts/CIK{info['cik']:010d}.json"))
            fin = parse(raw)
            if not fin["annual"]:
                raise ValueError("no annual financials in XBRL data")
        except Exception as ex:
            errors.append(f"{t}: {ex}")
            continue
        finally:
            time.sleep(0.2)  # SEC fair-access: max 10 requests/second

        an = analyze(fin, cfg)
        new_alerts += detect_changes(t, state["companies"].get(t), an, fin, cfg)
        state["companies"][t] = {"score": an["score"], "signals": [s["id"] for s in an["signals"]],
                                 "filing": fin["filing"]["accn"] if fin["filing"] else None}
        companies.append({"ticker": t, "cik": info["cik"], "name": info["name"], "fin": fin, "analysis": an})
    state["companies"] = {k: v for k, v in state["companies"].items() if k in tickers}
    return companies, new_alerts, errors


def run_discovery(cfg: dict, state: dict, errors: list[str]):
    if not cfg.get("discovery", True):
        return [], {}, []
    try:
        import discover
        universe = discover.run(cfg)
    except Exception as ex:
        errors.append(f"Discovery failed: {ex}")
        return [], {}, []
    disc = discover.lists(universe, cfg)

    prev = state.get("discovery")
    new_radar = []
    if prev:
        prev_radar = set(prev.get("radar", []))
        new_radar = [c for c in disc["radar"] if c["ticker"] not in prev_radar]
        prev_scores = prev.get("scores", {})
        rising = []
        for c in universe:
            ps = prev_scores.get(c["ticker"])
            if ps is not None and c["analysis"]["score"] - ps >= 10:
                c["prev_score"] = ps
                rising.append(c)
        disc["rising"] = sorted(rising, key=lambda c: -(c["analysis"]["score"] - c["prev_score"]))
    state["discovery"] = {"radar": [c["ticker"] for c in disc["radar"]],
                          "scores": {c["ticker"]: c["analysis"]["score"] for c in universe}}
    return universe, disc, new_radar


def main():
    cfg = load_config()
    tickers = load_tickers()
    DOCS.mkdir(exist_ok=True)
    state = json.loads(STATE_FILE.read_text(encoding="utf-8")) if STATE_FILE.exists() else {"companies": {}, "alerts": []}

    watch, watch_alerts, errors = run_watchlist(cfg, state, tickers)
    universe, disc, new_radar = run_discovery(cfg, state, errors)

    now = datetime.now(timezone.utc).isoformat(timespec="minutes")
    radar_alerts = [{"date": now, "ticker": c["ticker"], "tone": "info", "title": "New on your radar",
                     "body": f"Score {c['analysis']['score']} · Rev {pct(c['analysis']['metrics'].get('revenue_growth'), True)}"
                             f" · FCF margin {pct(c['analysis']['metrics'].get('fcf_margin'))} · {c['name']}"}
                    for c in new_radar[:30]]
    state["alerts"] = (list(reversed(watch_alerts + radar_alerts)) + state["alerts"])[:300]
    state["last_run"] = now

    generated = datetime.now(timezone.utc).strftime("%d %b %Y %H:%M UTC")
    (DOCS / "index.html").write_text(
        render(watch, disc, new_radar, len(universe), state["alerts"], errors, generated, cfg), encoding="utf-8")
    (DOCS / "universe.json").write_text(universe_json(universe), encoding="utf-8")
    STATE_FILE.write_text(json.dumps(state, separators=(",", ":")), encoding="utf-8")

    # ---- notifications
    by_ticker: dict[str, list[dict]] = {}
    for a in watch_alerts:
        by_ticker.setdefault(a["ticker"], []).append(a)
    for t, items in by_ticker.items():
        neg = any(i["tone"] in ("neg", "warn") for i in items)
        pos = any(i["tone"] == "pos" for i in items)
        tags = ["chart_with_upwards_trend"] if pos and not neg else ["warning"] if neg and not pos else ["bar_chart"]
        score = next((c["analysis"]["score"] for c in watch if c["ticker"] == t), "?")
        push(f"{t} · score {score}", "\n".join(f"{TONE_ICON.get(i['tone'], '•')} {i['title']}: {i['body']}" for i in items),
             tags, priority=4 if neg else 3)

    if new_radar and cfg.get("notify_discoveries", True):
        lines = [f"{c['ticker']} {c['analysis']['score']} · Rev {pct(c['analysis']['metrics'].get('revenue_growth'), True)}"
                 f" · {c['name'][:28]}" for c in new_radar[:15]]
        more = f"\n…and {len(new_radar) - 15} more" if len(new_radar) > 15 else ""
        push(f"🔎 {len(new_radar)} new compan{'y' if len(new_radar) == 1 else 'ies'} on your radar", "\n".join(lines) + more,
             ["mag"], priority=3)

    if cfg["daily_summary"] and (watch or universe):
        lines = [f"{c['ticker']} {c['analysis']['score']} ({c['analysis']['rating']})"
                 for c in sorted(watch, key=lambda c: -c["analysis"]["score"])[:8]]
        if universe:
            lines.append(f"Discover: {len(disc.get('radar', []))} of {len(universe)} companies score "
                         f"{cfg.get('discovery_min_score', 75)}+, {len(new_radar)} new today")
        head = f"{len(watch_alerts)} watchlist change(s)" if watch_alerts else "No watchlist changes"
        push(f"CompanyPulse · {head}", "\n".join(lines) + ("\n⚠ " + "; ".join(errors[:3]) if errors else ""),
             ["newspaper"], priority=2)

    print(f"\nDone: {len(watch)} watchlist, {len(universe)} screened, {len(new_radar)} new on radar, "
          f"{len(watch_alerts)} alerts, {len(errors)} errors.")
    for x in errors:
        print("  !", x)


if __name__ == "__main__":
    main()
