"""
Prices, valuation and timing.

- Nasdaq screener: one request -> price, market cap, sector and industry for every US stock.
- Nasdaq historical prices: 1 year of daily closes for US-listed watchlist tickers.
- European Central Bank: daily reference exchange rates.
All free, no API key. Failures never break the run - the affected numbers are just left empty.
"""
from __future__ import annotations

import json
import math
import re
import statistics
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

UA = "CompanyPulse/1.0 (personal stock screener)"


def _get(url: str, retries: int = 3) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.read()
        except Exception:
            if attempt == retries - 1:
                raise
            time.sleep(2 + attempt * 3)
    raise RuntimeError("unreachable")


def _get_json(url: str) -> dict:
    return json.loads(_get(url))


def norm(ticker: str) -> str:
    return ticker.upper().replace(".", "-").replace("/", "-").replace("^", "-")


def _num(s) -> float | None:
    try:
        v = float(str(s).replace("$", "").replace(",", "").replace("%", ""))
        return v if math.isfinite(v) else None
    except (TypeError, ValueError):
        return None

# --------------------------------------------------------------------------- data sources

def nasdaq_screener() -> dict[str, dict]:
    """All US-listed stocks in one request: price, market cap, sector, industry."""
    d = _get_json("https://api.nasdaq.com/api/screener/stocks?tableonly=true&download=true")
    out = {}
    for r in d["data"]["rows"]:
        mcap = _num(r.get("marketCap"))
        out[norm(r["symbol"])] = {
            "price": _num(r.get("lastsale")),
            "mcap": mcap if mcap and mcap > 0 else None,
            "day_pct": (_num(r.get("pctchange")) or 0) / 100 if r.get("pctchange") else None,
            "sector": (r.get("sector") or "").strip() or "Other",
            "industry": (r.get("industry") or "").strip(),
            "country": (r.get("country") or "").strip(),
        }
    return out


def nasdaq_history(symbol: str) -> dict:
    """About one year of daily closes for a US-listed ticker (oldest -> newest)."""
    start = (datetime.now(timezone.utc) - timedelta(days=380)).date().isoformat()
    sym = urllib.parse.quote(symbol.replace(".", "-"))
    d = _get_json(f"https://api.nasdaq.com/api/quote/{sym}/historical?assetclass=stocks&fromdate={start}&limit=400")
    rows = ((d.get("data") or {}).get("tradesTable") or {}).get("rows") or []
    closes = [_num(r["close"]) for r in reversed(rows)]
    closes = [c for c in closes if c]
    return {"currency": "USD", "price": closes[-1] if closes else None, "closes": closes}


_ecb: dict[str, float] | None = None


def fx(src: str, dst: str) -> float | None:
    """How many `dst` for 1 `src`, from the European Central Bank's daily reference rates."""
    global _ecb
    src, dst = src.upper(), dst.upper()
    if src == dst:
        return 1.0
    if _ecb is None:
        try:
            xml = _get("https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml").decode()
            _ecb = {m[0]: float(m[1]) for m in re.findall(r"currency='([A-Z]{3})' rate='([0-9.]+)'", xml)}
            _ecb["EUR"] = 1.0
        except Exception:
            _ecb = {}
    if src in _ecb and dst in _ecb:
        return _ecb[dst] / _ecb[src]
    return None

# --------------------------------------------------------------------------- technicals

def rsi(closes: list[float], n: int = 14) -> float | None:
    if len(closes) <= n:
        return None
    gains = losses = 0.0
    for i in range(1, n + 1):
        ch = closes[i] - closes[i - 1]
        gains += max(ch, 0)
        losses += max(-ch, 0)
    ag, al = gains / n, losses / n
    for i in range(n + 1, len(closes)):
        ch = closes[i] - closes[i - 1]
        ag = (ag * (n - 1) + max(ch, 0)) / n
        al = (al * (n - 1) + max(-ch, 0)) / n
    return 100.0 if al == 0 else 100 - 100 / (1 + ag / al)


def technicals(h: dict, price: float | None = None, day_pct: float | None = None) -> dict:
    """Trend and momentum from ~1 year of daily closes. `price` = latest price (e.g. from the screener)."""
    c = h["closes"]
    price = price or h["price"] or (c[-1] if c else None)
    if not c or not price:
        return {}
    hi, lo = max(c + [price]), min(c + [price])
    ma50 = sum(c[-50:]) / len(c[-50:]) if len(c) >= 50 else None
    ma200 = sum(c[-200:]) / 200 if len(c) >= 200 else None

    def ret(n):
        return price / c[-n] - 1 if len(c) >= n and c[-n] else None
    return {
        "currency": h.get("currency", "USD"), "price": price,
        "day_pct": day_pct if day_pct is not None else (c[-1] / c[-2] - 1 if len(c) >= 2 and c[-2] else None),
        "high52": hi, "low52": lo, "drawdown": price / hi - 1,
        "ma50": ma50, "ma200": ma200, "vs_ma200": price / ma200 - 1 if ma200 else None,
        "rsi": rsi(c + [price]),
        "ret_3m": ret(63), "ret_6m": ret(126), "ret_12m": ret(len(c)) if len(c) >= 240 else None,
    }


EUROPE = {"United Kingdom", "Netherlands", "Ireland", "Switzerland", "Germany", "France", "Spain", "Italy", "Belgium",
          "Luxembourg", "Denmark", "Sweden", "Norway", "Finland", "Austria", "Portugal", "Greece", "Poland", "Jersey",
          "Guernsey", "Isle of Man", "Monaco", "Cyprus", "Czech Republic", "Hungary", "Iceland", "Malta"}
ASIA = {"China", "Hong Kong", "Japan", "Taiwan", "South Korea", "India", "Singapore", "Indonesia", "Malaysia",
        "Philippines", "Thailand", "Vietnam", "Macau", "Kazakhstan", "Australia", "New Zealand"}


def region(country: str | None) -> str:
    if country == "United States":
        return "US"
    if country in EUROPE:
        return "Europe"
    if country in ASIA:
        return "Asia"
    return "Other"


# --------------------------------------------------------------------------- valuation

def ttm_net_income(fin: dict) -> float | None:
    """Trailing-twelve-month net income from the last 4 quarters, else last fiscal year."""
    q = [x for x in fin.get("quarterly", []) if x.get("net_income") is not None][-4:]
    if len(q) == 4 and (q[-1]["end"] - q[0]["end"]).days < 300:
        return sum(x["net_income"] for x in q)
    return fin["annual"][-1]["net_income"] if fin.get("annual") else None


def valuation(fin: dict, metrics: dict, mcap: float | None, sector_pe: float | None = None,
              sector: str | None = None) -> dict:
    """mcap must be in the same currency as the financial statements."""
    if not mcap or not fin.get("annual"):
        return {}
    cur = fin["annual"][-1]
    ni = ttm_net_income(fin)
    fcf = metrics.get("fcf")
    rev = cur.get("revenue")
    v = {
        "mcap": mcap,
        "pe": mcap / ni if ni and ni > 0 else None,
        # banks/insurers/brokers: cash flow is dominated by customer money, FCF yield is meaningless
        "fcf_yield": fcf / mcap if fcf is not None and sector != "Finance" else None,
        "ps": mcap / rev if rev and rev > 0 else None,
        "sector_pe": sector_pe,
    }
    div, bb = cur.get("dividends"), cur.get("buybacks")
    v["div_yield"] = abs(div) / mcap if div else None
    v["buyback_yield"] = abs(bb) / mcap if bb else None
    v["shareholder_yield"] = (v["div_yield"] or 0) + (v["buyback_yield"] or 0) if (div or bb) else None
    g = metrics.get("cagr3") if metrics.get("cagr3") is not None else metrics.get("revenue_growth")
    # cap growth at 25%/yr: past hyper-growth rarely continues and would make anything look cheap
    v["peg"] = v["pe"] / (min(g, 0.25) * 100) if v["pe"] and g and g > 0.02 else None

    # numbers that can't be right (currency or share-count mismatch in the source data)
    if (v["pe"] is not None and v["pe"] < 3) or (v["fcf_yield"] is not None and v["fcf_yield"] > 0.3):
        return {"mcap": mcap, "label": None, "signals": [], "note": "valuation data looks inconsistent"}

    cheap, rich, sig = 0, 0, []

    def add(sid, tone, title, detail):
        sig.append({"id": sid, "cat": "Valuation", "tone": tone, "title": title, "detail": detail, "weight": 0})

    fy = v["fcf_yield"]
    if fy is not None:
        if fy >= 0.06:
            cheap += 1
            add("val_fcf_yield", "pos", "High free-cash-flow yield", f"FCF yield {fy * 100:.1f}%: you get a lot of cash per euro/dollar invested.")
        elif fy < 0.015:
            rich += 1
            add("val_low_fcf_yield", "warn", "Low free-cash-flow yield", f"FCF yield {fy * 100:.1f}%: the price already assumes a lot of future growth.")
    if v["pe"] and sector_pe:
        if v["pe"] <= 0.8 * sector_pe:
            cheap += 1
            add("val_cheap_sector", "pos", "Cheaper than its sector", f"P/E {v['pe']:.1f} vs sector median {sector_pe:.1f}.")
        elif v["pe"] >= 1.5 * sector_pe:
            rich += 1
            add("val_rich_sector", "warn", "Expensive vs its sector", f"P/E {v['pe']:.1f} vs sector median {sector_pe:.1f}.")
    if ni is not None and ni <= 0:
        rich += 1
        add("val_no_earnings", "warn", "No earnings to value", "Loss-making: the price rests entirely on future expectations.")
    if v["peg"] is not None:
        if v["peg"] < 1.0:
            cheap += 1
            add("val_garp", "pos", "Growth at a reasonable price", f"PEG {v['peg']:.2f} (P/E divided by growth rate, below 1 is attractive).")
        elif v["peg"] > 3.0:
            rich += 1
            add("val_peg_high", "warn", "Price runs ahead of growth", f"PEG {v['peg']:.2f}.")
    shy = v["shareholder_yield"]
    if shy is not None and shy >= 0.05:
        add("val_shareholder_yield", "pos", "High shareholder yield",
            f"{shy * 100:.1f}% of the market value returned per year through dividends and buybacks.")
    net = cheap - rich
    v["label"] = "Attractive" if net >= 1 else "Expensive" if net <= -1 else "Fair"
    v["value_score"] = max(0, min(100, 50 + 17 * net))
    v["signals"] = sig
    return v


def sector_medians(rows: list[tuple[str, float]]) -> dict[str, float]:
    by: dict[str, list[float]] = {}
    for sector, pe in rows:
        if pe and 0 < pe < 200:
            by.setdefault(sector, []).append(pe)
    return {s: statistics.median(v) for s, v in by.items() if len(v) >= 8}

# --------------------------------------------------------------------------- DCA guidance + price alerts

def dca_action(score: int, val: dict, t: dict) -> tuple[str, str]:
    """Heuristic guidance for someone who invests regularly. Not advice."""
    label = val.get("label")
    dd, vs200, r = t.get("drawdown"), t.get("vs_ma200"), t.get("rsi")
    if score < 40:
        return "Review", "Business quality is weak. Check whether the investment thesis still holds before adding."
    if label == "Expensive" and ((vs200 or 0) > 0.25 or (r or 0) > 70):
        return "Wait", "Expensive and stretched above its trend. Consider pausing extra buys."
    on_sale = (dd is not None and dd <= -0.15) or (vs200 is not None and vs200 < 0) or (r is not None and r < 35)
    if score >= 70 and label != "Expensive" and on_sale:
        return "Buy extra", "Strong business, not expensive, and the price is temporarily down. Classic moment to add more."
    if score >= 55 and label != "Expensive":
        return "DCA as usual", "Healthy business at a reasonable price. Keep your regular buys."
    return "Hold", "Keep watching. Either quality is average or the price is high."


def price_flags(t: dict, action: str, today: str) -> dict[str, tuple[str, str, str]]:
    """Active price conditions -> (tone, title, body). Alerts fire when a flag newly appears."""
    f = {}
    dd, vs200, r, day = t.get("drawdown"), t.get("vs_ma200"), t.get("rsi"), t.get("day_pct")
    if action == "Buy extra":
        f["buyzone"] = ("pos", "Entered the buy-extra zone", "Quality business, fair price, and currently on sale.")
    if dd is not None and dd <= -0.25:
        f["dip25"] = ("warn", "25%+ below 52-week high", f"Down {dd * -100:.0f}% from its 1-year high.")
    elif dd is not None and dd <= -0.15:
        f["dip15"] = ("info", "15%+ below 52-week high", f"Down {dd * -100:.0f}% from its 1-year high.")
    if vs200 is not None and vs200 < 0:
        f["below200"] = ("info", "Below its 200-day average", f"Price {vs200 * 100:.1f}% under the long-term trend.")
    if r is not None and r < 30:
        f["oversold"] = ("info", "Oversold", f"RSI {r:.0f}: heavy recent selling, often near short-term lows.")
    if t.get("price") and t.get("low52") and t["price"] <= t["low52"] * 1.01:
        f["low52"] = ("warn", "At a 52-week low", "Lowest price in a year. Check the news before buying.")
    if day is not None and day <= -0.06:
        f[f"drop_{today}"] = ("warn", f"Dropped {day * -100:.1f}% today", "Big daily move. Check the news.")
    return f


def now_date() -> str:
    return datetime.now(timezone.utc).date().isoformat()


# --------------------------------------------------------------------------- own price history for every stock

def update_price_store(screener: dict[str, dict], path) -> dict[str, list[float]]:
    """Keep one closing price per day for every US-listed stock (~270 trading days), built from the daily
    screener snapshot. After ~6 months this gives momentum for the whole market without extra requests."""
    store: dict[str, dict[str, float]] = {}
    try:
        store = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        store = {}
    today = now_date()
    if datetime.now(timezone.utc).weekday() < 5:
        store[today] = {t: round(r["price"], 4) for t, r in screener.items() if r.get("price")}
    for k in sorted(store)[:-270]:
        del store[k]
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(store, separators=(",", ":")), encoding="utf-8")
    series: dict[str, list[float]] = {}
    for day in sorted(store):
        for t, p in store[day].items():
            series.setdefault(t, []).append(p)
    return series
