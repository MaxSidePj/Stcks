"""Tiny disk cache so heavy downloads happen at most once per day (persisted between runs by actions/cache)."""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

DIR = Path(__file__).resolve().parent / "cache"


def _path(key: str) -> Path:
    return DIR / (hashlib.sha1(key.encode()).hexdigest() + ".json")


def get(key: str, ttl_hours: float, fetch):
    """Return the cached value if younger than ttl, otherwise call fetch() and store it.
    If fetch fails and an older copy exists, the older copy is used."""
    DIR.mkdir(exist_ok=True)
    p = _path(key)
    old = None
    if p.exists():
        try:
            old = json.loads(p.read_text(encoding="utf-8"))
            if time.time() - old["t"] < ttl_hours * 3600:
                return old["v"]
        except Exception:
            old = None
    try:
        v = fetch()
    except Exception:
        if old is not None:
            return old["v"]
        raise
    p.write_text(json.dumps({"t": time.time(), "v": v}, separators=(",", ":"), default=str), encoding="utf-8")
    return v


def prune(max_age_days: float = 10):
    if not DIR.exists():
        return
    cutoff = time.time() - max_age_days * 86400
    for p in DIR.glob("*.json"):
        if p.stat().st_mtime < cutoff:
            p.unlink(missing_ok=True)
