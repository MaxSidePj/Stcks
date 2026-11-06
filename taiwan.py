"""Official Taiwan bulk open data. Keep partial financials separate from Buy rankings."""
from __future__ import annotations

import html
import json
import math
import re
import ssl
import time
import urllib.request
from datetime import date
from html.parser import HTMLParser

import cache
import market

TWSE = "https://openapi.twse.com.tw/v1"
TPEX = "https://www.tpex.org.tw/openapi/v1"
FED = "https://www.federalreserve.gov/releases/h10/current/"


def is_ticker(ticker):
    return bool(re.fullmatch(r"\d{4,6}\.(TW|TWO)", ticker))


def watch_alerts(rows, state, watched, cfg, now):
    previous = state.setdefault("taiwan_watch", {})
    alerts = []
    for c in rows:
        old = previous.get(c["ticker"])
        period, growth = c.get("monthly_period"), c.get("monthly_growth")
        if old and period and period != old.get("period") and cfg.get("notify_new_filings", True):
            alerts.append({"ticker": c["ticker"], "date": now, "tone": "info", "title": "New monthly revenue data",
                           "body": f"Period {period}; revenue growth {growth * 100:+.1f}% YoY" if growth is not None else f"Period {period}"})
        if old and growth is not None and growth >= cfg.get("revenue_growth_threshold", 0.1) and (old.get("growth") is None or old["growth"] < cfg.get("revenue_growth_threshold", 0.1)) and cfg.get("notify_positive", True):
            alerts.append({"ticker": c["ticker"], "date": now, "tone": "pos", "title": "Monthly revenue growing",
                           "body": f"Revenue {growth * 100:+.1f}% versus the same month last year"})
        previous[c["ticker"]] = {"period": period, "growth": growth}
    state["taiwan_watch"] = {t: value for t, value in previous.items() if t in watched}
    return alerts


def number(value):
    try:
        v = float(str(value).replace(",", "").strip())
        return v if math.isfinite(v) else None
    except (TypeError, ValueError):
        return None


def day(value):
    s = re.sub(r"[^0-9]", "", str(value or ""))
    try:
        if len(s) == 7:  # Republic of China calendar
            return date(int(s[:3]) + 1911, int(s[3:5]), int(s[5:])).isoformat()
        if len(s) == 8:
            return date(int(s[:4]), int(s[4:6]), int(s[6:])).isoformat()
    except ValueError:
        pass
    return None


def by_code(rows):
    return {str(r.get("公司代號") or r.get("SecuritiesCompanyCode") or r.get("Code")): r for r in rows}


def month(value):
    s = str(value or "").strip()
    if s.isdigit() and len(s) in (5, 6):
        year, mon = int(s[:-2]), int(s[-2:])
        if 1 <= mon <= 12:
            return f"{year + 1911 if year < 1911 else year:04d}-{mon:02d}"
    return s


def normalize(board, profiles, quotes, revenue, ratios=(), income=()):
    quotes, revenue, ratios, income = map(by_code, (quotes, revenue, ratios, income))
    out = []
    for code, p in by_code(profiles).items():
        # Issuer master excludes the ETFs, warrants and other securities in the quote feed.
        q, rev, val, inc = quotes.get(code, {}), revenue.get(code, {}), ratios.get(code, {}), income.get(code, {})
        price = number(q.get("ClosingPrice") or q.get("Close") or q.get("LatestPrice"))
        if price is not None and price <= 0:
            price = None
        shares = number(p.get("已發行普通股數或TDR原股發行股數") or p.get("IssueShares"))
        preferred = number(p.get("特別股") or p.get("PreferredStock.shares"))
        mcap = price * shares if price and shares and shares > 0 and not preferred else None
        current = number(rev.get("營業收入-當月營收"))
        previous = number(rev.get("營業收入-去年當月營收"))
        growth = current / previous - 1 if current is not None and previous and previous > 0 else None
        revenue_total = number(inc.get("營業收入"))
        profit = number(inc.get("本期淨利（淨損）"))
        quarter = str(inc.get("季別") or inc.get("Season") or "")
        year = number(inc.get("年度") or inc.get("Year"))
        financial_period = f"{int(year) + 1911 if year and year < 1911 else int(year)} Q{quarter} YTD" if year and quarter else None
        pe = number(val.get("PEratio") or val.get("PriceEarningRatio"))
        change = number(q.get("Change"))
        base = price - change if price is not None and change is not None else None
        name = p.get("公司名稱") or p.get("CompanyName") or code
        english = p.get("英文簡稱") or p.get("Symbol") or ""
        out.append({"ticker": code + (".TW" if board == "TWSE" else ".TWO"), "name": name,
                    "english_name": english, "region": "Asia", "country": "Taiwan", "exchange": board,
                    "source": "Taiwan open data", "currency": "TWD", "price": price,
                    "price_date": day(q.get("Date")), "market_cap_local": mcap, "market_cap_usd": None,
                    "market_cap_basis": "common shares × price; preferred-share issuers excluded",
                    "pe": pe if pe and pe > 0 else None,
                    "pb": number(val.get("PBratio") or val.get("PriceBookRatio")),
                    "day_pct": change / base if change is not None and base and base > 0 else None,
                    "listed": day(p.get("上市日期") or p.get("DateOfListing")),
                    "monthly_growth": growth, "monthly_revenue": current * 1000 if current is not None else None,
                    "monthly_period": month(rev.get("資料年月")),
                    "income_period": financial_period,
                    "net_margin": profit / revenue_total if profit is not None and revenue_total else None,
                    "sector": rev.get("產業別") or "Other", "health": None, "buy_score": None, "status": "",
                    "coverage": "Local price / partial financials", "momentum": None})
    return out


class _Rows(HTMLParser):
    def __init__(self):
        super().__init__()
        self.rows, self.cells, self.text, self.inside = [], [], [], False

    def handle_starttag(self, tag, attrs):
        if tag == "tr":
            self.cells = []
        if tag in ("td", "th"):
            self.text, self.inside = [], True

    def handle_data(self, text):
        if self.inside:
            self.text.append(text)

    def handle_endtag(self, tag):
        if tag in ("td", "th"):
            self.cells.append(" ".join("".join(self.text).split()))
            self.inside = False
        if tag == "tr" and self.cells:
            self.rows.append(self.cells)


def twd_rate(document):
    parser = _Rows()
    parser.feed(document)
    for row in parser.rows:
        if row and row[0].strip().upper() == "TAIWAN":
            values = [number(v) for v in row[2:]]
            return next((v for v in reversed(values) if v and v > 0), None)
    return None


def history(rows, path):
    """One observation per actual source date. Never manufacture historical prices."""
    store = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    for c in rows:
        if not c["price"] or not c["price_date"]:
            continue
        observations = store.setdefault(c["ticker"], {})
        observations[c["price_date"]] = c["price"]
        observations = dict(sorted(observations.items())[-270:])
        store[c["ticker"]] = observations
        values = list(observations.values())
        # Splits/dividends aren't adjusted in this open-data feed. Show raw return only,
        # with an explicit label; exclude it entirely when a large discontinuity occurs.
        jump = any(abs(b / a - 1) > 0.35 for a, b in zip(values, values[1:]) if a)
        if len(values) >= 126 and not jump:
            c["momentum"] = values[-1] / values[-126] - 1
        c["momentum_note"] = "Observed unadjusted 6-month price return; distributions/splits can affect it" if c["momentum"] is not None else "Building price history; no adjusted momentum available yet"
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(store, separators=(",", ":")), encoding="utf-8")


def run(cfg):
    end = time.monotonic() + min(120, max(1, cfg.get("taiwan_seconds", 120)))
    remaining = min(14, max(0, cfg.get("taiwan_request_limit", 14)))
    errors = []

    def download(url):
        nonlocal remaining
        if remaining <= 0 or time.monotonic() >= end - 1:
            raise TimeoutError("Taiwan download budget reached")
        remaining -= 1
        # certifi supplies a maintained trust store; certificate checks stay enabled.
        try:
            import certifi
            context = ssl.create_default_context(cafile=certifi.where())
        except ImportError:
            context = ssl.create_default_context()
        req = urllib.request.Request(url, headers={"User-Agent": "CompanyPulse/1.0", "Accept": "application/json,text/html"})
        with urllib.request.urlopen(req, timeout=min(20, max(1, end - time.monotonic())), context=context) as response:
            return response.read().decode("utf-8-sig")

    def data(base, path):
        try:
            result = cache.get("tw-bulk:" + path, 24, lambda: json.loads(download(base + path)))
            if not isinstance(result, list):
                raise ValueError("unexpected exchange response")
            return result
        except Exception as ex:
            errors.append(f"Taiwan {path}: {type(ex).__name__}")
            return []

    boards = [
        ("TWSE", TWSE, "/opendata/t187ap03_L", "/exchangeReport/STOCK_DAY_ALL", "/opendata/t187ap05_L", "/exchangeReport/BWIBBU_ALL", "/opendata/t187ap06_L_ci"),
        ("TPEx", TPEX, "/mopsfin_t187ap03_O", "/tpex_mainboard_daily_close_quotes", "/mopsfin_t187ap05_O", "/tpex_mainboard_peratio_analysis", "/mopsfin_t187ap06_O_ci"),
        ("TPEx Emerging", TPEX, "/mopsfin_t187ap03_R", "/tpex_esb_latest_statistics", "/t187ap05_R", None, None),
    ]
    rows = []
    for board, base, *paths in boards:
        rows.extend(normalize(board, *(data(base, path) if path else [] for path in paths)))
    try:
        rate = cache.get("twd-usd-fed", 24, lambda: twd_rate(download(FED)))
        for c in rows:
            if rate and c["market_cap_local"]:
                c["market_cap_usd"] = c["market_cap_local"] / rate
    except Exception as ex:
        errors.append(f"Taiwan USD size conversion unavailable ({type(ex).__name__}); local values retained")
    history(rows, cache.DIR / "taiwan-history.json")
    return rows, errors
