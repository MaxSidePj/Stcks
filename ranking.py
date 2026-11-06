"""
The Buy Score: one number (0-100) combining four factors, computed in two investing styles.

  Quality  - health of the business (growth, margins, cash flow, balance sheet, dilution)  = health score
  Value    - is the price reasonable (FCF yield, P/E vs sector, PEG, shareholder yield)   = value score
  Growth   - revenue growth and acceleration of the latest quarter
  Momentum - 6/12-month price trend, position vs 50/200-day averages, not overheated

  Quality value    = 40% quality + 35% value + 10% growth + 15% momentum
  Growth momentum  = 20% quality + 10% value + 40% growth + 30% momentum
  Buy score        = the higher of the two (the style is shown), then adjusted for events:
                     insider buying (+), activist stake (+), negative news (-), red flags (cap), pending takeover (excluded)

Momentum is relative strength: a stock's 6/12-month return ranked against all other analysed stocks (0-100).
Statuses are selective: Strong buy = score >= 80 and among the best `strong_buy_max` (15) of the day;
Buy = score >= 70 and within the best `buy_max` (40); Watch >= 58; otherwise Avoid.
"""
from __future__ import annotations

from datetime import date

STATUS_ORDER = ["Strong buy", "Buy", "Watch", "Avoid", "Deal pending"]


def clamp(v, lo=0.0, hi=100.0):
    return max(lo, min(hi, v))


def growth_score(m: dict) -> float:
    g = m.get("revenue_growth")
    if g is None:
        return 40.0
    s = 50 + 100 * clamp(g, -0.3, 0.5)               # +50% growth -> 100, flat -> 50, -30% -> 20
    qg = m.get("q_growth")
    if qg is not None:
        if qg > g + 0.05:
            s += 10                                   # accelerating
        elif qg < g - 0.10:
            s -= 8                                    # decelerating
    if m.get("fcf") is not None and m["fcf"] < 0 and (m.get("op_margin") or 0) < 0:
        s -= 10                                       # growth bought with cash burn
    return clamp(s)


def add_relative_strength(techs: list[dict]) -> None:
    """Percentile rank (0-100) of the blended 6/12-month return across all analysed stocks."""
    pts = [t for t in techs if t and t.get("ret_6m") is not None]
    raw = sorted(0.6 * t["ret_6m"] + 0.4 * (t["ret_12m"] if t.get("ret_12m") is not None else t["ret_6m"]) for t in pts)
    if len(raw) < 20:
        return
    import bisect
    for t in pts:
        v = 0.6 * t["ret_6m"] + 0.4 * (t["ret_12m"] if t.get("ret_12m") is not None else t["ret_6m"])
        t["rs"] = 100 * bisect.bisect_left(raw, v) / (len(raw) - 1)


def momentum_score(t: dict) -> float | None:
    if not t or t.get("ret_6m") is None:
        return None
    s = t["rs"] if t.get("rs") is not None else 50 + 40 * clamp(t["ret_6m"], -0.5, 0.5)
    if t.get("ma200") and t.get("ma50"):
        if t["price"] > t["ma200"] and t["ma50"] > t["ma200"]:
            s += 6                                    # healthy uptrend
        elif t["price"] < t["ma200"] and t["ma50"] < t["ma200"]:
            s -= 6                                    # downtrend
    if (t.get("rsi") or 50) > 78 or (t.get("vs_ma200") or 0) > 0.6:
        s -= 12                                       # overheated: don't chase
    return clamp(s)


def score(c: dict) -> dict:
    """c has analysis (quality), val (valuation), tech (price), ev (events). Returns ranking dict."""
    q = float(c["analysis"]["score"])
    val = c.get("val") or {}
    v = float(val["value_score"]) if val.get("value_score") is not None else 50.0
    m = c["analysis"]["metrics"]
    g = growth_score(m)
    mo = momentum_score(c.get("tech") or {})
    mo_used = mo if mo is not None else 50.0

    qv = 0.40 * q + 0.35 * v + 0.10 * g + 0.15 * mo_used
    gm = 0.20 * q + 0.10 * v + 0.40 * g + 0.30 * mo_used
    style = "Quality value" if qv >= gm else "Growth momentum"
    s = max(qv, gm)
    why, risks = [], []

    # guard rails
    if q < 35:
        s = min(s, 60)
        risks.append("Weak fundamentals (health score below 35)")
    if style == "Growth momentum" and mo is not None and mo < 45:
        s -= 5
        risks.append("Price trend not confirming the growth story yet")
    if val.get("label") == "Expensive" and style == "Quality value":
        s -= 3

    ev = c.get("ev") or {}
    ins = (ev.get("insiders") or {})
    if ins.get("value", 0) >= 100_000:
        bonus = 6 if len(ins.get("buyers", [])) >= 2 else 3
        s += bonus
        why.append(f"Insiders bought ${ins['value'] / 1e6:.1f}M of shares with their own money "
                   f"({len(ins['buyers'])} insider{'s' if len(ins['buyers']) > 1 else ''}, last 90 days)")
    if ev.get("activist"):
        s += 2
        why.append(f"Large / activist stake filed ({ev['activist']['form']}, {ev['activist']['date']})")
    news_flags = {f for n in ev.get("news", []) for f in n.get("flags", [])}
    if "neg" in news_flags:
        s -= 4
        risks.append("Negative keywords in recent headlines: read the news")
    if "deal" in news_flags:
        risks.append("Headlines mention an acquisition or merger: check whether it is buying or being bought")
    for rf in ev.get("red_flags", []):
        s = min(s, 45)
        risks.append(f"Red flag: {rf['text']} ({rf['date']})")

    # reasons in plain language
    if q >= 75:
        why.append(f"Excellent business quality (health {int(q)})")
    elif q >= 60:
        why.append(f"Solid business quality (health {int(q)})")
    if val.get("label") == "Attractive":
        bits = []
        if val.get("fcf_yield") is not None:
            bits.append(f"FCF yield {val['fcf_yield'] * 100:.1f}%")
        if val.get("pe"):
            bits.append(f"P/E {val['pe']:.1f}" + (f" vs sector {val['sector_pe']:.1f}" if val.get("sector_pe") else ""))
        why.append("Attractively priced (" + ", ".join(bits) + ")")
    elif val.get("label") == "Expensive":
        risks.append("Valuation is expensive: more downside if growth disappoints")
    if (m.get("revenue_growth") or 0) >= 0.2:
        why.append(f"Revenue growing {m['revenue_growth'] * 100:.0f}% per year")
    if m.get("q_growth") is not None and m.get("revenue_growth") is not None and m["q_growth"] > m["revenue_growth"] + 0.05:
        why.append(f"Growth accelerating: latest quarter +{m['q_growth'] * 100:.0f}% YoY")
    t = c.get("tech") or {}
    if mo is not None:
        if mo >= 70:
            r12 = f", {t['ret_12m'] * 100:+.0f}% over 12 months" if t.get("ret_12m") is not None else ""
            beats = f"outperforms {t['rs']:.0f}% of analysed stocks: " if t.get("rs") is not None else ""
            why.append(f"Strong relative strength ({beats}{t['ret_6m'] * 100:+.0f}% over 6 months{r12})")
        elif t.get("drawdown") is not None and t["drawdown"] <= -0.15 and q >= 70:
            why.append(f"Quality company {t['drawdown'] * -100:.0f}% below its 1-year high: buying opportunity if the thesis holds")
        if (t.get("rsi") or 50) > 78:
            risks.append(f"Overheated short term (RSI {t['rsi']:.0f}): consider waiting for a pullback")
        if (t.get("vs_ma200") or 0) > 0.6:
            risks.append(f"Price is {t['vs_ma200'] * 100:.0f}% above its 200-day average: big run already, "
                         "buy in smaller steps and expect sharp swings")
    last_end = c["fin"]["annual"][-1]["end"] if c["fin"].get("annual") else None
    if last_end and (date.today() - last_end).days > 450:
        s -= 3
        risks.append(f"Latest annual figures are for the year ending {last_end}; newer results are not in the SEC data yet")
    if (m.get("share_change") or 0) >= 0.05:
        risks.append(f"Share count up {m['share_change'] * 100:.0f}% in a year (dilution)")
    if m.get("fcf") is not None and m["fcf"] < 0:
        risks.append("Still burning cash: depends on funding")

    s = clamp(s)
    status = "Watch" if s >= 58 else "Avoid"      # final Strong buy / Buy decided by assign_statuses()
    if ev.get("takeover"):
        status = "Deal pending"
        risks.insert(0, f"Takeover / merger filing ({ev['takeover']['form']}, {ev['takeover']['date']}): price is tied to the deal")
    return {"score": round(s), "status": status, "style": style, "q": round(q), "v": round(v), "g": round(g),
            "m": None if mo is None else round(mo), "why": why[:5], "risks": risks[:5]}


def assign_statuses(cands: list[dict], cfg: dict) -> None:
    """Make statuses selective: only the best few of the day can be Strong buy / Buy."""
    strong_max, buy_max = cfg.get("strong_buy_max", 15), cfg.get("buy_max", 40)
    n_strong = n_buy = 0
    for c in sorted(cands, key=lambda c: -c["rank"]["score"]):
        rk = c["rank"]
        if rk["status"] == "Deal pending":
            continue
        s = rk["score"]
        mcap = (c.get("market") or {}).get("mcap") or 0
        if any(r.startswith("Red flag") for r in rk["risks"]):
            rk["status"] = "Avoid"
        elif s >= 80 and n_strong < strong_max and rk["q"] >= 60 and rk["m"] is not None and mcap >= 5e8 and (c.get("val") or {}).get("value_score") is not None:
            rk["status"], n_strong = "Strong buy", n_strong + 1
        elif s >= 70 and n_strong + n_buy < buy_max and rk["m"] is not None and (c.get("val") or {}).get("value_score") is not None:
            rk["status"], n_buy = "Buy", n_buy + 1
        elif s >= 58:
            rk["status"] = "Watch"
        else:
            rk["status"] = "Avoid"


def prelim(c: dict) -> tuple[float, float]:
    """Quality-value and growth-momentum scores without price trend / events (used to pick the shortlist)."""
    q = float(c["analysis"]["score"])
    v = float((c.get("val") or {}).get("value_score") or 50)
    g = growth_score(c["analysis"]["metrics"])
    return 0.40 * q + 0.35 * v + 0.10 * g + 7.5, 0.20 * q + 0.10 * v + 0.40 * g + 15
