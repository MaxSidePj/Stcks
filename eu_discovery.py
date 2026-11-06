"""Progressive ESEF discovery: no ticker feed, price claims or paid dependencies."""
from __future__ import annotations

import json
import os
import time
import re
import unicodedata
import urllib.error
import urllib.request
from datetime import date, datetime, timezone

import cache
import europe
from discover import _fin_from_json, _fin_to_json


class Budget:
    def __init__(self, requests=50, seconds=180):
        self.left = requests
        self.end = time.monotonic() + seconds

    def request(self):
        remaining = self.end - time.monotonic()
        if self.left <= 0 or remaining < 1:
            raise TimeoutError("European discovery budget reached")
        self.left -= 1
        return min(30, remaining)


def index(year, budget):
    """Daily index refresh; conditional requests follow the provider's guidance."""
    path = cache.DIR / f"esef-index-{year}.json"
    old = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    if time.time() - old.get("checked", 0) < 86400:
        return old["document"]
    contact = os.environ.get("SEC_CONTACT_EMAIL") or os.environ.get("GITHUB_REPOSITORY", "personal stock screener")
    headers = {"User-Agent": f"CompanyPulse/1.0 ({contact})"}
    if old.get("modified"):
        headers["If-Modified-Since"] = old["modified"]
    req = urllib.request.Request(f"{europe.BASE}/indexes/filings-{year}.json", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=budget.request()) as response:
            old = {"document": json.loads(response.read()), "modified": response.headers.get("Last-Modified")}
    except urllib.error.HTTPError as ex:
        if ex.code == 404:
            old = {"document": {"data": [], "included": []}}
        elif ex.code != 304 or "document" not in old:
            raise
    old["checked"] = time.time()
    cache.DIR.mkdir(exist_ok=True)
    path.write_text(json.dumps(old, separators=(",", ":")), encoding="utf-8")
    return old["document"]


def catalogue(documents, countries):
    entities, filings = {}, []
    for doc in documents:
        entities.update({r["id"]: r["attributes"] for r in doc.get("included", []) if r["type"] == "entity"})
        filings.extend(doc.get("data", []))
    out = {}
    for filing in filings:
        attr = filing["attributes"]
        entity = entities.get(filing["relationships"]["entity"]["data"]["id"], {})
        lei = entity.get("identifier")
        if not lei or attr.get("country") not in countries or not attr.get("json_url"):
            continue
        item = out.setdefault(lei, {"name": entity.get("name") or lei, "country": attr["country"], "filings": []})
        item["filings"].append(filing)
    return out


def canonical_name(name):
    """Exact normalized legal name; no substring or fuzzy matching between issuers."""
    value = unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode().upper()
    value = re.sub(r"\b([A-Z])\s*[./]\s*([A-Z])\b", r"\1\2", value)
    words = re.findall(r"[A-Z0-9]+", value)
    legal = {"SE", "SA", "NV", "AB", "AG", "AS", "ASA", "PLC", "LTD", "LIMITED", "INC", "CORP", "CORPORATION", "OYJ", "SPA"}
    while len(words) > 1 and words[-1] in legal:
        words.pop()
    return "".join(words)


def name_index(entries):
    candidates = {}
    for lei, item in entries.items():
        key = canonical_name(item["name"])
        if key:
            candidates.setdefault(key, set()).add(lei)
    return {key: next(iter(leis)) for key, leis in candidates.items() if len(leis) == 1}


def pending_rows(saved):
    """All catalogue issuers are searchable before their reports have been downloaded."""
    return [{"ticker": "EU:" + lei, "name": item["name"], "region": "Europe", "country": item["country"],
             "exchange": "ESEF", "source": "European annual reports", "currency": None, "price": None,
             "market_cap_usd": None, "health": None, "buy_score": None, "status": "", "pe": None,
             "coverage": "Catalogue issuer; financials queued",
             "financial_reason": saved.get("issues", {}).get(lei) or "Financial reports load automatically in bounded batches; no watchlist entry is required."}
            for lei, item in saved.get("catalogue", {}).items()]


def share_reports(universe, watch, state):
    for c in watch:
        lei = c.get("fin", {}).get("lei")
        if c.get("region_src") != "EU-only" or not lei:
            continue
        saved = state.setdefault("esef_discovery", {"results": {}, "cursor": 0})
        saved["results"][lei] = {"name": c["name"], "country": c.get("country") or "Unknown", "fin": _fin_to_json(c["fin"])}
        existing = next((i for i, other in enumerate(universe) if other.get("fin", {}).get("lei") == lei), None)
        if existing is None:
            universe.append(c)
        else:
            universe[existing] = c


def run(cfg, state):
    import pulse
    saved = state.setdefault("esef_discovery", {"results": {}, "cursor": 0})
    today = datetime.now(timezone.utc).date()
    errors = []
    budget = Budget(min(50, max(0, int(cfg.get("esef_request_limit", 50)))),
                    min(180, max(1, int(cfg.get("esef_seconds", 180)))))
    if saved.get("scan_date") != today.isoformat():
        saved.update(scan_date=today.isoformat(), scanned_today=0)
    daily_left = max(0, min(200, int(cfg.get("esef_companies_per_day", 200))) - saved["scanned_today"])
    if daily_left and saved.get("failed_date") != today.isoformat():
        try:
            countries = cfg.get("esef_countries", ["BE", "FR", "NL", "SE", "DE", "IT", "ES", "DK", "FI", "NO", "IE", "AT", "PT", "PL"])
            entries = catalogue([index(y, budget) for y in range(today.year - 2, today.year + 1)], countries)
            keys = sorted(entries)
            saved["catalogue_size"] = len(keys)
            saved["catalogue"] = {lei: {"name": item["name"], "country": item["country"]} for lei, item in entries.items()}
            issues = saved.setdefault("issues", {})
            count = min(len(keys), daily_left, max(0, min(40, int(cfg.get("esef_companies_per_run", 40)))))
            for _ in range(count):
                if budget.left <= 0 or time.monotonic() >= budget.end - 1:
                    break
                pos = saved["cursor"] % len(keys)
                lei = keys[pos]
                item = entries[lei]
                saved["cursor"] = (pos + 1) % len(keys)
                saved["scanned_today"] += 1
                try:
                    # One annual report normally contains comparative periods. This doubles
                    # issuer throughput without raising the HTTP or time budget.
                    fin = europe.fetch(lei, filings=item["filings"], budget=budget, report_limit=1)
                    if len(fin["annual"]) < 2 or not fin["annual"][-1].get("revenue"):
                        issues[lei] = "Report checked: insufficient standard-tagged revenue history for a health analysis."
                        continue
                    saved["results"][lei] = {"name": item["name"], "country": item["country"], "fin": _fin_to_json(fin)}
                    issues.pop(lei, None)
                except Exception as ex:
                    issues[lei] = f"Report fetch or parsing failed: {type(ex).__name__}"
                    errors.append(f"ESEF {item['name']}: {ex}")
        except Exception as ex:
            errors.append(f"European catalogue unavailable: {ex}")
            # Avoid repeated downloads every two hours during an outage.
            saved["failed_date"] = today.isoformat()
    cfg["_esef_name_index"] = name_index(saved.get("catalogue", {}))
    companies = []
    for lei, item in list(saved["results"].items()):
        fin = _fin_from_json(item["fin"])
        if (today - fin["annual"][-1]["end"]).days > cfg.get("max_data_age_days", 700):
            del saved["results"][lei]
            continue
        companies.append({"ticker": "EU:" + lei, "name": item["name"], "country": item["country"],
                          "region": "Europe", "region_src": "EU-only", "exchange": "ESEF", "cik": None,
                          "fin": fin, "analysis": pulse.analyze(fin, cfg)})
    return companies, errors
