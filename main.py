"""
OffGamers Weekly Product Events Tracker — Cloud Factory edition
- Reads Tracker.xlsx from the repo root
- Scrapes each URL with Firecrawl
- Analyses content with Claude (following PROMPT-CHAT.md / SKILL.md rules)
- Renders index.html (sortable dashboard) + summary.md
"""

import os
import re
import sys
import json
import time
import datetime
import pandas as pd
import anthropic
from firecrawl import FirecrawlApp
from jinja2 import Template

# ── API keys from environment / GitHub Secrets ───────────────────────────────
FIRECRAWL_API_KEY = os.environ.get("FIRECRAWL_API_KEY")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")

if not FIRECRAWL_API_KEY or not ANTHROPIC_API_KEY:
    sys.exit("ERROR: FIRECRAWL_API_KEY and ANTHROPIC_API_KEY must be set as environment variables.")

firecrawl_app = FirecrawlApp(api_key=FIRECRAWL_API_KEY)
claude        = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

# ── Auto-detect model — tries each in order until one works ──────────────────
MODELS_TO_TRY = [
    "claude-haiku-4-5-20251001",
    "claude-sonnet-4-6",
    "claude-opus-4-6",
    "claude-3-5-sonnet-20241022",
]
MODEL = MODELS_TO_TRY[0]

# ── Constants ─────────────────────────────────────────────────────────────────
TRACKER_FILE = "Tracker.xlsx"
OUTPUT_HTML  = "index.html"
OUTPUT_MD    = "summary.md"
SCRAPE_DELAY = 2.0
MAX_CONTENT  = 6000
MAX_COMBINED = 10000

today        = datetime.date.today()
window_start = today - datetime.timedelta(days=10)
window_end   = today + datetime.timedelta(days=10)


# ── URL helpers ───────────────────────────────────────────────────────────────

def extract_clean_url(raw_value):
    if pd.isna(raw_value):
        return None
    val = str(raw_value).strip()
    if not val or val == "nan":
        return None
    if val.startswith(("http://", "https://")):
        return re.split(r"\s", val)[0].rstrip(".,;)")
    m = re.search(r"https?://[^\s\)\]\n,;]+", val)
    return m.group(0).rstrip(".,;)") if m else None


def get_urls_for_row(row):
    urls = []
    for col in ["Official URL", "URL 2", "URL 3"]:
        url = extract_clean_url(row.get(col))
        if url and url not in urls:
            urls.append(url)
    if not urls:
        custom = str(row.get("Custom Instruction", "")).strip()
        if custom and custom != "nan":
            for url in re.findall(r"https?://[^\s\)\]\n,;]+", custom):
                url = url.rstrip(".,;)")
                if url not in urls:
                    urls.append(url)
                if len(urls) == 3:
                    break
    return urls


# ── Scraping helper ───────────────────────────────────────────────────────────

def scrape_url(url):
    try:
        result = firecrawl_app.scrape_url(url, params={"formats": ["markdown"]})
        if isinstance(result, dict):
            text = result.get("markdown", "") or ""
            if result.get("error"):
                print(f"      Firecrawl error: {result['error']}")
        else:
            text = getattr(result, "markdown", "") or ""
        text = text.strip()
        print(f"      Scraped {url[:60]} -> {len(text)} chars")
        if len(text) < 100:
            print(f"      WARNING: Very short content — site may be blocking")
        return text
    except Exception as exc:
        print(f"      ERROR scraping {url[:60]}: {str(exc)[:150]}")
        return f"UNABLE_TO_ACCESS: {str(exc)[:200]}"


# ── Claude helpers ────────────────────────────────────────────────────────────

def ask_claude(prompt):
    """Try each model in order until one works on this account."""
    global MODEL
    for model in MODELS_TO_TRY:
        try:
            response = claude.messages.create(
                model=model,
                max_tokens=1024,
                messages=[{"role": "user", "content": prompt}],
            )
            MODEL = model
            return response.content[0].text.strip()
        except Exception as exc:
            if "not_found" in str(exc) or "404" in str(exc):
                print(f"    Model {model} not available, trying next...")
                continue
            raise
    raise Exception("No Claude model found — check your API key permissions")


def parse_json_from_claude(raw):
    original = raw
    if "```" in raw:
        parts = raw.split("```")
        if len(parts) >= 2:
            raw = parts[1]
            if raw.lower().startswith("json"):
                raw = raw[4:]
    raw = raw.strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    for start_char, end_char in [('{', '}'), ('[', ']')]:
        start = raw.find(start_char)
        end   = raw.rfind(end_char)
        if start != -1 and end != -1 and end > start:
            try:
                return json.loads(raw[start:end+1])
            except json.JSONDecodeError:
                pass
    try:
        return json.loads(original.strip())
    except json.JSONDecodeError:
        raise ValueError(f"Could not parse JSON. Claude said: {original[:200]}")


# ── PASS 1 — News Monitor rows ────────────────────────────────────────────────

def run_news_monitor_pass(df):
    news_rows     = df[df["Category"] == "News Monitor"]
    product_rows  = df[df["Category"] != "News Monitor"]
    product_names = product_rows["Product Name"].dropna().tolist()
    results       = []

    for _, row in news_rows.iterrows():
        site_name = str(row.get("Product Name", "")).strip()
        region    = str(row.get("Region",       "")).strip()
        url       = extract_clean_url(row.get("Official URL"))

        if not url:
            print(f"  [News Monitor] Skipping {site_name} — no URL")
            continue

        print(f"  [News Monitor] Scraping {site_name}...")
        content = scrape_url(url)
        time.sleep(SCRAPE_DELAY)

        if content.startswith("UNABLE_TO_ACCESS"):
            results.append({"source_name": site_name, "region": region,
                            "scan_count": 0, "error": content, "matches": []})
            continue

        prompt = f"""You are scanning a news website for the OffGamers weekly product tracker.

Today: {today}
Date window: {window_start} to {window_end} (plus and minus 10 days)
News site: {site_name}  |  Region: {region}

PRODUCT LIST (match case-insensitively, allow short forms):
{json.dumps(product_names, indent=2)}

NEWS CONTENT:
{content[:MAX_CONTENT]}

Return ONLY valid JSON, no markdown fences:
{{"matches": [{{"matched_product": "name", "event_region": "region", "event_name": "label", "indirect_brand_news": "under 25 words + source name", "announced_on": "YYYY-MM-DD or blank", "duration": "YYYY-MM-DD to YYYY-MM-DD or blank"}}]}}

If no matches: {{"matches": []}}"""

        try:
            data    = parse_json_from_claude(ask_claude(prompt))
            matches = data.get("matches", []) if isinstance(data, dict) else []
        except Exception as exc:
            print(f"    WARNING parse error for {site_name}: {exc}")
            matches = []

        results.append({"source_name": site_name, "region": region,
                        "scan_count": len(matches), "matches": matches})
        print(f"    -> {len(matches)} match(es)")

    return results


# ── PASS 2 — Product rows ─────────────────────────────────────────────────────

def run_product_pass(df):
    product_rows = df[df["Category"] != "News Monitor"]
    results      = []

    for _, row in product_rows.iterrows():
        product_name       = str(row.get("Product Name",       "")).strip()
        category           = str(row.get("Category",           "")).strip()
        region             = str(row.get("Region",             "")).strip()
        tier               = str(row.get("Tier",               "")).strip()
        custom_instruction = str(row.get("Custom Instruction", "")).strip()
        urls               = get_urls_for_row(row)

        base = {
            "product_name": product_name, "category": category, "region": region,
            "tier": tier, "last_checked": str(today),
            "event_region": "", "event_name": "", "direct_brand_news": "",
            "announced_on": "", "duration": "", "unable_to_access": False,
        }

        if not urls:
            print(f"  [Product] {product_name} — no URL")
            base.update({"direct_brand_news": "No URL provided", "unable_to_access": True})
            results.append(base)
            continue

        print(f"  [Product] {product_name} ({len(urls)} URL(s))...")

        scraped_parts = []
        all_blocked   = True
        for url in urls:
            content = scrape_url(url)
            time.sleep(SCRAPE_DELAY)
            if content.startswith("UNABLE_TO_ACCESS"):
                scraped_parts.append(f"[Source: {url}]\n{content}")
            else:
                scraped_parts.append(f"[Source: {url}]\n{content[:MAX_CONTENT]}")
                if len(content) >= 100:
                    all_blocked = False

        combined = "\n\n---\n\n".join(scraped_parts)[:MAX_COMBINED]

        if all_blocked:
            print(f"    -> All URLs blocked — marking Unable to access")
            base.update({"direct_brand_news": "Unable to access", "unable_to_access": True})
            results.append(base)
            continue

        if custom_instruction and custom_instruction != "nan":
            task_section = f"CUSTOM INSTRUCTION (follow exactly):\n{custom_instruction}"
        else:
            task_section = """Look for items ACTIVE OR ANNOUNCED within the date window:
- New product launches
- Bonus events (bonus credit, double rewards)
- Discounts / flash sales
- Discontinuations / region exits / expiry deadlines
CONSOLIDATION: multiple sub-events under one campaign = ONE row with summary."""

        prompt = f"""You are analysing product pages for the OffGamers weekly tracker.

Today: {today}
Date window: {window_start} to {window_end} (plus and minus 10 days, look both directions)

Product: {product_name} | Category: {category} | Region: {region}

{task_section}

RULES:
- If content is insufficient or blocked: set unable_to_access to true
- Never invent findings. Empty event columns are valid.
- Duration format: YYYY-MM-DD to YYYY-MM-DD

SCRAPED CONTENT:
{combined}

Return ONLY valid JSON, no markdown fences:
{{"event_region": "region or blank", "event_name": "3-5 word label or blank", "direct_brand_news": "summary or Unable to access", "announced_on": "YYYY-MM-DD or blank", "duration": "YYYY-MM-DD to YYYY-MM-DD or blank", "unable_to_access": false}}"""

        try:
            data = parse_json_from_claude(ask_claude(prompt))
            if not isinstance(data, dict):
                raise ValueError("Expected JSON object")
        except Exception as exc:
            print(f"    WARNING parse error for {product_name}: {exc}")
            data = {"event_region": "", "event_name": "",
                    "direct_brand_news": f"Parse error: {str(exc)[:100]}",
                    "announced_on": "", "duration": "", "unable_to_access": False}

        base.update({
            "event_region":      data.get("event_region",      ""),
            "event_name":        data.get("event_name",        ""),
            "direct_brand_news": data.get("direct_brand_news", ""),
            "announced_on":      data.get("announced_on",      ""),
            "duration":          data.get("duration",          ""),
            "unable_to_access":  bool(data.get("unable_to_access", False)),
        })
        results.append(base)
        print(f"    -> {base['event_name'] or '(no event)'}")

    return results


# ── Summary markdown ──────────────────────────────────────────────────────────

def build_summary_md(product_results, news_matches):
    launches     = [r for r in product_results if any(w in (r.get("event_name") or "").lower() for w in ["launch","new","release","open"])]
    bonuses      = [r for r in product_results if any(w in (r.get("event_name") or "").lower() for w in ["bonus","discount","sale","promo","reward","offer"])]
    discontinues = [r for r in product_results if any(w in (r.get("event_name") or "").lower() for w in ["discontinu","expir","exit","end","remov","clos"])]
    unable       = [r for r in product_results if r.get("unable_to_access") or "unable to access" in (r.get("direct_brand_news") or "").lower()]
    zero         = [r for r in product_results if not r.get("event_name") and not r.get("unable_to_access")]
    indirect     = [(nm["source_name"], m) for nm in news_matches for m in nm.get("matches", [])]

    def fmt(r):
        line = f"- {r['product_name']}"
        if r.get("event_name"): line += f": {r['event_name']}"
        if r.get("duration"):   line += f", {r['duration']}"
        return line

    lines = [f"# OffGamers Tracker - {today}\n",
             "## New launches this week"]
    lines += [fmt(r) for r in launches] or ["(none this week)"]
    lines += ["\n## Active bonuses / discounts"]
    lines += [fmt(r) for r in bonuses] or ["(none this week)"]
    lines += ["\n## Discontinuations or expiries"]
    lines += [fmt(r) for r in discontinues] or ["(none this week)"]
    lines += ["\n## Top 3 indirect brand news items"]
    lines += [f"{i}. {m.get('indirect_brand_news','')}" for i,(s,m) in enumerate(indirect[:3],1)] or ["(none this week)"]
    lines += ["\n## Products with zero findings"]
    lines += [", ".join(r["product_name"] for r in zero) or "(none)"]
    lines += ["\n## Products where the page could not be accessed"]
    lines += [f"- {r['product_name']}: {r.get('direct_brand_news','')}" for r in unable] or ["(none this week)"]

    return "\n".join(lines)


# ── HTML generation ───────────────────────────────────────────────────────────

def row_css_class(r):
    name = (r.get("event_name")        or "").lower()
    news = (r.get("direct_brand_news") or "").lower()
    if r.get("unable_to_access") or "unable to access" in news:
        return "unable"
    if any(w in name for w in ["discontinu","expir","exit","end","remov","clos"]):
        return "discontinue"
    if any(w in name for w in ["bonus","discount","sale","promo","reward","offer"]):
        return "bonus"
    if r.get("event_name"):
        return "event"
    return "no-event"


HTML_TEMPLATE = Template(r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>OffGamers Weekly Tracker - {{ today }}</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:Arial,Helvetica,sans-serif;font-size:14px;background:#f5f6fa;color:#2d2d2d}
header{background:#1a1a2e;color:#fff;padding:20px 32px}
header h1{font-size:22px;font-weight:700}
header p{color:#aaa;font-size:13px;margin-top:4px}
.container{max-width:1500px;margin:0 auto;padding:24px 32px}
.cards{display:flex;gap:16px;margin-bottom:28px;flex-wrap:wrap}
.card{background:#fff;border-radius:8px;padding:16px 20px;flex:1;min-width:140px;box-shadow:0 1px 4px rgba(0,0,0,.08)}
.card .val{font-size:28px;font-weight:700;color:#1a1a2e}
.card .lbl{font-size:12px;color:#888;margin-top:4px}
.section{background:#fff;border-radius:8px;box-shadow:0 1px 4px rgba(0,0,0,.08);margin-bottom:28px;overflow:hidden}
.section-header{background:#1a1a2e;color:#fff;padding:12px 20px;font-size:15px;font-weight:600}
.legend{display:flex;gap:16px;padding:10px 20px;font-size:12px;color:#555;border-bottom:1px solid #eee;flex-wrap:wrap}
.legend span{display:flex;align-items:center;gap:5px}
.dot{width:10px;height:10px;border-radius:2px;display:inline-block}
.filters{padding:12px 20px;border-bottom:1px solid #eee;display:flex;gap:10px;flex-wrap:wrap;align-items:center}
.filters label{font-size:12px;color:#666;font-weight:700}
.filters select,.filters input{border:1px solid #ddd;border-radius:4px;padding:5px 9px;font-size:13px}
table{width:100%;border-collapse:collapse}
thead th{background:#f0f2f8;padding:10px 12px;text-align:left;font-size:12px;font-weight:700;color:#555;cursor:pointer;white-space:nowrap;user-select:none;border-bottom:2px solid #ddd}
thead th:hover{background:#e3e7f2}
thead th.sorted-asc::after{content:" ^"}
thead th.sorted-desc::after{content:" v"}
tbody tr:nth-child(even){background:#fafbfd}
tbody tr:hover{background:#eef2ff}
td{padding:9px 12px;border-bottom:1px solid #f0f0f0;vertical-align:top}
.news-cell{max-width:340px;font-size:13px;line-height:1.5}
.badge{background:#e8eaf6;color:#3949ab;border-radius:12px;padding:2px 8px;font-size:11px;font-weight:600;white-space:nowrap}
.muted{color:#bbb}
.center{text-align:center;padding:20px}
tr.row-event       td:first-child{border-left:4px solid #4CAF50}
tr.row-bonus       td:first-child{border-left:4px solid #FF9800}
tr.row-discontinue td:first-child{border-left:4px solid #F44336}
tr.row-unable      td:first-child{border-left:4px solid #9E9E9E}
tr.row-no-event    td:first-child{border-left:4px solid #e0e0e0}
</style>
</head>
<body>
<header>
  <h1>OffGamers Weekly Product Events Tracker</h1>
  <p>Run date: {{ today }} | Window: {{ window_start }} to {{ window_end }} | Model: {{ model }}</p>
</header>
<div class="container">
  <div class="cards">
    <div class="card"><div class="val">{{ total_products }}</div><div class="lbl">Products Tracked</div></div>
    <div class="card"><div class="val">{{ events_found }}</div><div class="lbl">Events Found</div></div>
    <div class="card"><div class="val">{{ total_indirect }}</div><div class="lbl">Indirect News</div></div>
    <div class="card"><div class="val">{{ no_events }}</div><div class="lbl">No Events</div></div>
    <div class="card"><div class="val">{{ unable_count }}</div><div class="lbl">Unable to Access</div></div>
  </div>
  <div class="section">
    <div class="section-header">Product Events</div>
    <div class="legend">
      <span><span class="dot" style="background:#4CAF50"></span>Event</span>
      <span><span class="dot" style="background:#FF9800"></span>Bonus/Discount</span>
      <span><span class="dot" style="background:#F44336"></span>Discontinuation</span>
      <span><span class="dot" style="background:#9E9E9E"></span>Unable to Access</span>
      <span><span class="dot" style="background:#e0e0e0"></span>No Event</span>
    </div>
    <div class="filters">
      <label>Filter:</label>
      <input id="searchInput" type="text" placeholder="Search product..." oninput="filterProducts()">
      <select id="catFilter" onchange="filterProducts()"><option value="">All Categories</option></select>
      <select id="regionFilter" onchange="filterProducts()"><option value="">All Regions</option></select>
      <select id="statusFilter" onchange="filterProducts()">
        <option value="">All Statuses</option>
        <option value="row-event">Has Event</option>
        <option value="row-bonus">Has Bonus</option>
        <option value="row-discontinue">Discontinuation</option>
        <option value="row-unable">Unable to Access</option>
        <option value="row-no-event">No Event</option>
      </select>
    </div>
    <table>
      <thead><tr>
        <th onclick="sortTable('productTbody',0)">Product Name</th>
        <th onclick="sortTable('productTbody',1)">Category</th>
        <th onclick="sortTable('productTbody',2)">Region</th>
        <th onclick="sortTable('productTbody',3)">Event Name</th>
        <th onclick="sortTable('productTbody',4)">Event Region</th>
        <th onclick="sortTable('productTbody',5)">Direct Brand News</th>
        <th onclick="sortTable('productTbody',6)">Announced On</th>
        <th onclick="sortTable('productTbody',7)">Duration</th>
        <th onclick="sortTable('productTbody',8)">Last Checked</th>
      </tr></thead>
      <tbody id="productTbody">
        {% for r in product_results %}
        <tr class="row-{{ r.css_class }}" data-category="{{ r.category }}" data-region="{{ r.region }}">
          <td>{{ r.product_name }}</td>
          <td><span class="badge">{{ r.category }}</span></td>
          <td>{{ r.region }}</td>
          <td>{% if r.event_name %}{{ r.event_name }}{% else %}<span class="muted">-</span>{% endif %}</td>
          <td>{% if r.event_region %}{{ r.event_region }}{% else %}<span class="muted">-</span>{% endif %}</td>
          <td class="news-cell">{% if r.direct_brand_news %}{{ r.direct_brand_news }}{% else %}<span class="muted">-</span>{% endif %}</td>
          <td>{% if r.announced_on %}{{ r.announced_on }}{% else %}<span class="muted">-</span>{% endif %}</td>
          <td>{% if r.duration %}{{ r.duration }}{% else %}<span class="muted">-</span>{% endif %}</td>
          <td>{{ r.last_checked }}</td>
        </tr>
        {% endfor %}
      </tbody>
    </table>
  </div>
  <div class="section">
    <div class="section-header">Indirect Brand News</div>
    <table>
      <thead><tr>
        <th onclick="sortTable('newsTbody',0)">Product</th>
        <th onclick="sortTable('newsTbody',1)">Source</th>
        <th onclick="sortTable('newsTbody',2)">Region</th>
        <th onclick="sortTable('newsTbody',3)">Event Label</th>
        <th onclick="sortTable('newsTbody',4)">News</th>
        <th onclick="sortTable('newsTbody',5)">Announced On</th>
        <th onclick="sortTable('newsTbody',6)">Duration</th>
      </tr></thead>
      <tbody id="newsTbody">
        {% set ns = namespace(count=0) %}
        {% for nm in news_matches %}{% for m in nm.matches %}{% set ns.count = ns.count + 1 %}
          <tr>
            <td>{{ m.matched_product }}</td>
            <td>{{ nm.source_name }}</td>
            <td>{{ nm.region }}</td>
            <td>{{ m.event_name or '-' }}</td>
            <td class="news-cell">{{ m.indirect_brand_news }}</td>
            <td>{{ m.announced_on or '-' }}</td>
            <td>{{ m.duration or '-' }}</td>
          </tr>
        {% endfor %}{% endfor %}
        {% if ns.count == 0 %}
          <tr><td colspan="7" class="muted center">No indirect brand news found this week.</td></tr>
        {% endif %}
      </tbody>
    </table>
  </div>
</div>
<script>
(function(){
  const rows=document.querySelectorAll('#productTbody tr');
  const cats=new Set(),regs=new Set();
  rows.forEach(r=>{cats.add(r.dataset.category);regs.add(r.dataset.region);});
  const catSel=document.getElementById('catFilter');
  const regSel=document.getElementById('regionFilter');
  [...cats].filter(Boolean).sort().forEach(c=>{const o=document.createElement('option');o.value=c;o.textContent=c;catSel.appendChild(o);});
  [...regs].filter(Boolean).sort().forEach(r=>{const o=document.createElement('option');o.value=r;o.textContent=r;regSel.appendChild(o);});
})();
function filterProducts(){
  const search=document.getElementById('searchInput').value.toLowerCase();
  const cat=document.getElementById('catFilter').value;
  const reg=document.getElementById('regionFilter').value;
  const status=document.getElementById('statusFilter').value;
  document.querySelectorAll('#productTbody tr').forEach(row=>{
    const name=row.cells[0].textContent.toLowerCase();
    const rowCls=[...row.classList].find(c=>c.startsWith('row-'))||'';
    const show=(!search||name.includes(search))&&(!cat||row.dataset.category===cat)&&(!reg||row.dataset.region===reg)&&(!status||rowCls===status);
    row.style.display=show?'':'none';
  });
}
const sortDir={};
function sortTable(tbodyId,col){
  const key=tbodyId+col;
  const asc=!sortDir[key];
  sortDir[key]=asc;
  const tbody=document.getElementById(tbodyId);
  const rows=[...tbody.querySelectorAll('tr')];
  rows.sort((a,b)=>{
    const ta=(a.cells[col]?.textContent||'').trim();
    const tb=(b.cells[col]?.textContent||'').trim();
    return asc?ta.localeCompare(tb):tb.localeCompare(ta);
  });
  rows.forEach(r=>tbody.appendChild(r));
  tbody.closest('table').querySelectorAll('th').forEach((th,i)=>{
    th.classList.remove('sorted-asc','sorted-desc');
    if(i===col)th.classList.add(asc?'sorted-asc':'sorted-desc');
  });
}
</script>
</body>
</html>""")


def generate_html(product_results, news_matches):
    for r in product_results:
        r["css_class"] = row_css_class(r)
    kw = lambda r, words: any(w in (r.get("event_name") or "").lower() for w in words)
    return HTML_TEMPLATE.render(
        today=today, window_start=window_start, window_end=window_end, model=MODEL,
        product_results=product_results, news_matches=news_matches,
        launches    =[r for r in product_results if kw(r,["launch","new","release","open"])],
        bonuses     =[r for r in product_results if kw(r,["bonus","discount","sale","promo","reward","offer"])],
        discontinues=[r for r in product_results if kw(r,["discontinu","expir","exit","end","remov","clos"])],
        all_indirect=[(nm["source_name"],m) for nm in news_matches for m in nm.get("matches",[])],
        total_products=len(product_results),
        events_found  =sum(1 for r in product_results if r.get("event_name")),
        unable_count  =sum(1 for r in product_results if r.get("unable_to_access")),
        no_events     =sum(1 for r in product_results if not r.get("event_name") and not r.get("unable_to_access")),
        total_indirect=sum(len(nm.get("matches",[])) for nm in news_matches),
    )


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    print(f"OffGamers Weekly Tracker - run date: {today}")
    print(f"Date window: {window_start} to {window_end}\n")

    df = pd.read_excel(TRACKER_FILE)
    print(f"Loaded {len(df)} rows from {TRACKER_FILE}\n")

    print("=== Pass 1: News Monitor ===")
    news_matches = run_news_monitor_pass(df)

    print("\n=== Pass 2: Products ===")
    product_results = run_product_pass(df)

    print("\nRendering index.html...")
    with open(OUTPUT_HTML, "w", encoding="utf-8") as fh:
        fh.write(generate_html(product_results, news_matches))

    print("Writing summary.md...")
    md = build_summary_md(product_results, news_matches)
    with open(OUTPUT_MD, "w", encoding="utf-8") as fh:
        fh.write(md)

    print(f"\n{md}\n")
    print(f"Done! Model used: {MODEL}")


if __name__ == "__main__":
    main()
