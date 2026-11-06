"""A searchable inventory that preserves companies with incomplete financial history."""
from __future__ import annotations

import html
import json
from datetime import datetime, timezone

import market


def size(cap):
    if cap is None:
        return "Unknown"
    return "Micro" if cap < 300e6 else "Small" if cap < 2e9 else "Mid" if cap < 10e9 else "Large"


def build(universe, screener, listing, local, pending=(), failures=None):
    loaded_leis = {c["fin"].get("lei") for c in universe} | {c["ticker"][3:] for c in universe if c["ticker"].startswith("EU:")}
    rows = {c["ticker"]: c for c in pending if c["ticker"].removeprefix("EU:") not in loaded_leis}
    allowed = {market.norm(v["ticker"]): v for v in listing.values()}
    for symbol, quote in screener.items():
        if symbol not in allowed:
            continue
        info = allowed[symbol]
        rows[info["ticker"]] = {"ticker": info["ticker"], "name": info["name"], "region": market.region(quote.get("country")),
            "country": quote.get("country") or "Unknown", "exchange": info["exchange"], "source": "US listings / SEC",
            "currency": "USD", "price": quote.get("price"), "market_cap_usd": quote.get("mcap"),
            "price_date": "Current Nasdaq snapshot", "coverage": "Quote only; financial history unavailable",
            "health": None, "buy_score": None, "status": "", "day_pct": quote.get("day_pct"), "pe": None,
            "financial_reason": (failures or {}).get(info["ticker"])}
    for c in universe:
        metrics = c["analysis"]["metrics"]
        mk, rank, tech, val = c.get("market") or {}, c.get("rank") or {}, c.get("tech") or {}, c.get("val") or {}
        complete = bool(rank) and tech.get("ret_6m") is not None and val.get("value_score") is not None
        row = rows.get(c["ticker"], {})
        row.pop("financial_reason", None)
        row.update(ticker=c["ticker"], name=c["name"], region=c.get("region") or "Other", country=c.get("country") or row.get("country") or "Unknown",
                   exchange=c.get("exchange") or "ESEF", source="European annual reports" if c.get("region_src") == "EU-only" else "US listings / SEC",
                   currency=tech.get("currency") or ("USD" if mk else c["fin"]["currency"]), price=tech.get("price") or mk.get("price"),
                   price_date="Current Nasdaq snapshot" if mk else None, market_cap_usd=mk.get("mcap"),
                   health=c["analysis"]["score"], buy_score=rank.get("score") if complete else None, status=rank.get("status", ""),
                   pe=val.get("pe"), annual_growth=metrics.get("revenue_growth"), fcf_margin=metrics.get("fcf_margin"),
                   net_margin=metrics.get("net_margin"), momentum=tech.get("ret_6m"),
                   financial_period=str(c["fin"]["annual"][-1]["end"]), financial_years=len(c["fin"]["annual"]),
                   coverage="Full ranking" if complete else "Annual financials" if c.get("region_src") == "EU-only" else "Financials; preliminary analysis")
        rows[c["ticker"]] = row
    for row in local:
        rows.setdefault(row["ticker"], row)  # main-board rows win if snapshots overlap a board migration
    for row in rows.values():
        row["size"] = size(row.get("market_cap_usd"))
    return list(rows.values())


def local_card(c, edit):
    e = html.escape
    def val(v):
        return "–" if v is None else f"{v:,.2f}"
    return (f'<details class="card"><summary><div class="who"><b>{e(c["ticker"])} · {e(c.get("english_name") or c["name"])}</b>'
            f'<small>{e(c["exchange"])} · partial financials · TWD {val(c.get("price"))}</small></div></summary>'
            f'<div class="body"><p>Price date: {e(c.get("price_date") or "unavailable")}. '
            f'P/E: {val(c.get("pe"))}. Monthly revenue growth: {val(c["monthly_growth"] * 100 if c.get("monthly_growth") is not None else None)}%.</p>'
            '<p class="foot">No full health or Buy rating: annual cash-flow history is not available in this feed. '
            'Taiwan TWSE / TPEx open data, Open Government Data License v1.0.</p>'
            + (f'<a href="{e(edit)}">Remove {e(c["ticker"])} from watchlist…</a>'
               '<p class="foot">Delete its line in companies.txt and commit.</p>' if edit else '') + '</div></details>')


HTML = """
<p class="desc">Search every available company, including entries too new or incomplete for a score. Blank means unavailable.
Sizes use USD market value: micro &lt;$300M, small $300M–$2B, mid $2B–$10B, large $10B+.</p>
<div class="filters">
<div class="full"><input id="cq" placeholder="Ticker, local name or English name"></div>
<div><label>Region</label><select id="cr"><option value="">All</option><option>US</option><option>Europe</option><option>Asia</option><option>Other</option></select></div>
<div><label>Size</label><select id="cs"><option value="">All sizes</option><option>Micro</option><option>Small</option><option>Mid</option><option>Large</option><option>Unknown</option></select></div>
<div><label>Source</label><select id="cd"><option value="">All</option><option>US listings / SEC</option><option>European annual reports</option><option>Taiwan open data</option></select></div>
<div><label>Coverage</label><select id="cc"><option value="">Any</option><option value="price">Has a price</option><option value="rank">Full ranking</option><option value="partial">No full ranking</option><option value="new">Recently listed (date known)</option><option value="short">Limited / unknown financial history</option></select></div>
<div><label>Annual growth at least %</label><input id="cag" type="number" inputmode="decimal" placeholder="Any"></div>
<div><label>Monthly growth at least %</label><input id="cmg" type="number" inputmode="decimal" placeholder="Any"></div>
<div class="full"><label>Sort</label><select id="co"><option value="market_cap_usd">Market value</option><option value="annual_growth">Annual revenue growth</option><option value="monthly_growth">Monthly revenue growth</option><option value="listed">Newest known listing</option><option value="health">Health</option><option value="buy_score">Buy score</option><option value="momentum">6-month price return</option><option value="pe">Lowest P/E</option></select></div>
</div><p class="desc" id="catalogue-count"></p><div id="catalogue-list"></div><button id="catalogue-more" hidden>Show more</button>
<p class="foot">Taiwan prices, profiles, monthly revenue and ratios: Taiwan Stock Exchange (TWSE) / Taipei Exchange (TPEx),
2026 bulk open datasets, made available under the <a href="https://data.gov.tw/license">Open Government Data License v1.0</a>.
USD size conversion uses the Federal Reserve H.10 Taiwan-dollar reference rate. Local price history is unadjusted; it is kept out of Buy ratings.</p>
"""

JS = r"""
let C=null, shown=100;
async function loadCatalogue(){if(!C){$('#catalogue-count').textContent='Loading companies…';try{C=await(await fetch('catalogue.json')).json();}catch(e){$('#catalogue-count').textContent='Could not load companies. Reload after the next successful check.';return;}}drawCatalogue();}
const cv=(v,d=2)=>v==null?'–':Number(v).toLocaleString(undefined,{maximumFractionDigits:d});
const catalogueSearch=s=>String(s).normalize('NFKD').toLowerCase().replace(/[^\p{L}\p{N}]+/gu,'');
function catalogueCard(c){const cap=c.market_cap_usd, name=c.english_name?c.english_name+' · '+c.name:c.name;
 const watchLine=c.ticker+' = '+c.name;
 return `<details class="card"><summary><div class="who"><b>${esc(c.ticker.startsWith('EU:')?c.name:c.ticker)}</b> ${c.status?`<span class="status ${stc[c.status]||''}">${esc(c.status)}</span>`:''}
 <small>${esc(name)}</small><div class="chips"><span class="chip">${esc(c.region)} · ${esc(c.size)}</span><span class="chip">${esc(c.currency||'')} ${cv(c.price)}</span>
 ${c.annual_growth!=null?`<span class="chip">Annual rev ${pct(c.annual_growth,1)}</span>`:''}${c.monthly_growth!=null?`<span class="chip">Monthly rev ${pct(c.monthly_growth,1)}</span>`:''}</div></div></summary>
 <div class="body"><p class="foot">${esc(c.coverage)} · ${esc(c.exchange||'')} · ${esc(c.country||'')}</p>${c.financial_reason?`<p class="foot">${esc(c.financial_reason)}</p>`:''}<div class="grid">
 ${[['Price',esc(c.currency||'')+' '+cv(c.price)],['Price date',esc(c.price_date||'unavailable')],['Market value (USD)',cap==null?'–':money(cap)],['P/E',cv(c.pe)],['P/B',cv(c.pb)],['Health',cv(c.health,0)],['Buy score',cv(c.buy_score,0)],['6m price return',pct(c.momentum,1)],['Net margin',pct(c.net_margin)],['FCF margin',pct(c.fcf_margin)],['Financial year end',esc(c.financial_period||'unavailable')],['Income period',esc(c.income_period||'unavailable')],['Monthly revenue period',esc(c.monthly_period||'unavailable')],['Listing date',esc(c.listed||'unknown')]].map(([a,b])=>`<div><small>${a}</small><b>${b}</b></div>`).join('')}</div>
 ${c.momentum_note?`<p class="foot">${esc(c.momentum_note)}</p>`:''}
 ${C.edit?`<div class="links"><a href="${esc(C.edit)}">Edit watchlist</a></div><p class="foot">Add this line: <code>${esc(watchLine)}</code>. To remove it, delete its line and commit.</p>`:''}</div></details>`;}
function drawCatalogue(){if(!C)return;const q=catalogueSearch($('#cq').value.trim()),reg=$('#cr').value,size=$('#cs').value,src=$('#cd').value,cov=$('#cc').value,key=$('#co').value;
 const ag=$('#cag').value,mg=$('#cmg').value,cut=new Date();cut.setFullYear(cut.getFullYear()-2);
 const rows=C.rows.filter(c=>(!q||catalogueSearch(c.ticker+' '+c.name+' '+(c.english_name||'')).includes(q))&&(!reg||c.region===reg)&&(!size||c.size===size)&&(!src||c.source===src)
 &&(!cov||(cov==='price'?c.price!=null:cov==='rank'?c.buy_score!=null:cov==='partial'?c.buy_score==null:cov==='short'?c.financial_years==null||c.financial_years<=1:c.listed&&new Date(c.listed)>=cut))
 &&(ag===''||c.annual_growth!=null&&c.annual_growth>=+ag/100)&&(mg===''||c.monthly_growth!=null&&c.monthly_growth>=+mg/100))
 .sort((a,b)=>{if(a[key]==null)return b[key]==null?0:1;if(b[key]==null)return -1;if(key==='listed')return b[key].localeCompare(a[key]);return key==='pe'?a[key]-b[key]:b[key]-a[key];});
 $('#catalogue-count').textContent=`${rows.length} of ${C.rows.length} available companies · showing ${Math.min(shown,rows.length)}`;
 $('#catalogue-list').innerHTML=rows.slice(0,shown).map(catalogueCard).join('')||'<p class="empty">No matches.</p>';
 $('#catalogue-more').hidden=shown>=rows.length;}
['#cq','#cr','#cs','#cd','#cc','#cag','#cmg','#co'].forEach(id=>$(id).addEventListener('input',()=>{shown=100;drawCatalogue();}));
$('#catalogue-more').onclick=()=>{shown+=100;drawCatalogue();};
"""
