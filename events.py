"""
Corporate events, insider buying and news for the shortlist + watchlist.

- SEC submissions (official): 8-K material events, takeover/merger filings, activist stakes, red flags.
- SEC Form 4 (official): open-market insider purchases (insiders buying with their own money).
- Google News RSS (public feed): latest headlines, scanned for important keywords.
"""
from __future__ import annotations

import re
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

import cache
import pulse as core

ITEM_LABELS = {
    "1.01": "Major agreement signed", "1.02": "Major agreement terminated", "1.03": "Bankruptcy / receivership",
    "2.01": "Acquisition or sale completed", "2.02": "Results published", "2.03": "New debt", "2.05": "Restructuring costs",
    "2.06": "Impairment (write-down)", "3.01": "Delisting / listing-rule notice", "3.02": "Unregistered share sale",
    "4.01": "Auditor changed", "4.02": "Past financials no longer reliable (restatement)", "5.01": "Change in control",
    "5.02": "Executive / director change", "7.01": "Investor presentation", "8.01": "Other material event",
}
RED_FLAG_ITEMS = {"1.03": "Bankruptcy filing", "3.01": "Delisting notice", "4.02": "Accounting restatement", "4.01": "Auditor change"}
# filings typically made by (or about) a company that is being acquired / taken private
TAKEOVER_FORMS = {"SC TO-T", "SC TO-T/A", "SC 14D9", "SC 14D9/A", "DEFM14A", "PREM14A", "DEFM14C", "SC 13E3", "SC 13E3/A"}
ACTIVIST_FORMS = {"SC 13D", "SC 13D/A"}

NEWS_FLAGS = [
    ("deal", "pos", r"\b(to acquire|acquires|acquisition of|to be acquired|takeover|buyout|merger|agrees to buy|tender offer|go private)\b"),
    ("neg", "neg", r"\b(investigation|probe|lawsuit|sued|fraud|short seller|downgrade[sd]?|cuts? (its )?guidance|lowers? (its )?(guidance|outlook)|recall|subpoena|delist|bankruptcy|misses|plunges?)\b"),
    ("pos", "pos", r"\b(upgrade[sd]?|raises? (its )?(guidance|outlook)|record (revenue|quarter)|beats|wins? .{0,30}contract|partnership with|buyback|surges?|soars?)\b"),
]


def _sec_json(url: str) -> dict:
    import json
    return json.loads(core.sec_get(url))


def sec_events(cik: int, days: int = 90) -> dict:
    """Recent material events from the SEC filing index."""
    sub = cache.get(f"sub:{cik}", 6, lambda: _sec_json(f"https://data.sec.gov/submissions/CIK{cik:010d}.json"))
    time.sleep(0.12)
    r = sub.get("filings", {}).get("recent", {})
    since = (date.today() - timedelta(days=days)).isoformat()
    out = {"events": [], "red_flags": [], "takeover": None, "activist": None, "form4": []}
    for i, form in enumerate(r.get("form", [])):
        fd = r["filingDate"][i]
        if fd < since:
            break
        accn = r["accessionNumber"][i]
        url = f"https://www.sec.gov/Archives/edgar/data/{cik}/{accn.replace('-', '')}/{r['primaryDocument'][i].split('/')[-1]}"
        if form in ("8-K", "6-K"):
            items = [x.strip() for x in (r.get("items", [""] * (i + 1))[i] or "").split(",") if x.strip()]
            labels = [ITEM_LABELS[x] for x in items if x in ITEM_LABELS and x not in ("9.01",)]
            if form == "6-K":
                if any(e["text"].startswith("Foreign issuer report") for e in out["events"]):
                    continue  # foreign companies file many routine 6-Ks: show only the latest
                labels = ["Foreign issuer report (6-K)"]
            if labels:
                out["events"].append({"date": fd, "text": ", ".join(labels), "url": url})
            for x in items:
                if x in RED_FLAG_ITEMS:
                    out["red_flags"].append({"date": fd, "text": RED_FLAG_ITEMS[x], "url": url})
        elif form in TAKEOVER_FORMS and not out["takeover"]:
            out["takeover"] = {"date": fd, "form": form, "url": url}
        elif form in ACTIVIST_FORMS and not out["activist"]:
            out["activist"] = {"date": fd, "form": form, "url": url}
        elif form == "4" and len(out["form4"]) < 8:
            out["form4"].append({"accn": accn, "doc": r["primaryDocument"][i].split("/")[-1], "date": fd})
    out["events"] = out["events"][:6]
    return out


def insider_buys(cik: int, form4: list[dict]) -> dict:
    """Open-market purchases (code P) by insiders in the recent Form 4 filings."""
    buyers, total = set(), 0.0
    for f in form4:
        url = f"https://www.sec.gov/Archives/edgar/data/{cik}/{f['accn'].replace('-', '')}/{f['doc']}"

        def fetch():
            root = ET.fromstring(core.sec_get(url))
            name = root.findtext(".//rptOwnerName") or "?"
            buys = 0.0
            for tx in root.findall(".//nonDerivativeTransaction"):
                if tx.findtext(".//transactionCode") == "P":
                    try:
                        buys += float(tx.findtext(".//transactionShares/value") or 0) * \
                            float(tx.findtext(".//transactionPricePerShare/value") or 0)
                    except ValueError:
                        pass
            return {"name": name, "buys": buys}
        try:
            res = cache.get(f"f4:{f['accn']}", 24 * 365, fetch)
            time.sleep(0.12)
        except Exception:
            continue
        if res["buys"] > 0:
            buyers.add(res["name"])
            total += res["buys"]
    return {"buyers": sorted(buyers), "value": total}


def news(query: str, limit: int = 5) -> list[dict]:
    url = ("https://news.google.com/rss/search?" +
           urllib.parse.urlencode({"q": f'"{query}" stock when:14d', "hl": "en-US", "gl": "US", "ceid": "US:en"}))

    def fetch():
        req = urllib.request.Request(url, headers={"User-Agent": "CompanyPulse/1.0 (personal stock screener)"})
        with urllib.request.urlopen(req, timeout=30) as r:
            root = ET.fromstring(r.read())
        items = []
        for it in root.findall(".//item")[:limit]:
            try:
                when = parsedate_to_datetime(it.findtext("pubDate")).astimezone(timezone.utc).date().isoformat()
            except Exception:
                when = ""
            items.append({"title": it.findtext("title") or "", "url": it.findtext("link") or "", "date": when})
        return items
    try:
        items = cache.get(f"news:{query}", 4, fetch)
        time.sleep(0.5)
    except Exception:
        return []
    # drop quote/price pages that are not news
    items = [it for it in items if not re.search(r"stock price|price (history|chart)|stock quote|quote &|stock analysis|share price today|chart",
                                                 it["title"], re.I)]
    for it in items:
        it["flags"] = [k for k, _, rx in NEWS_FLAGS if re.search(rx, it["title"], re.I)]
    return items


def news_link(query: str) -> str:
    return "https://news.google.com/search?" + urllib.parse.urlencode({"q": f"{query} stock", "hl": "en-US"})


def enrich(cik: int | None, query: str) -> dict:
    """Everything event-related for one company."""
    ev = {"events": [], "red_flags": [], "takeover": None, "activist": None, "insiders": {"buyers": [], "value": 0.0}}
    if cik:
        try:
            ev.update(sec_events(cik))
            ev["insiders"] = insider_buys(cik, ev.pop("form4", []))
        except Exception:
            pass
    ev["news"] = news(query)
    ev["news_link"] = news_link(query)
    return ev
