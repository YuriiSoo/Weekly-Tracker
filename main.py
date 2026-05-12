"""
OffGamers Weekly Product Events Tracker — Cloud Factory edition
- Reads Tracker.xlsx from the repo root
- Scrapes each URL with Firecrawl
- Analyses content with Claude 3.5 Sonnet, following SKILL.md (±10-day window)
- Renders index.html using a Jinja2 template
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

# ── API keys come from environment / GitHub Secrets ──────────────────────────
FIRECRAWL_API_KEY = os.environ.get("FIRECRAWL_API_KEY")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")

if not FIRECRAWL_API_KEY or not ANTHROPIC_API_KEY:
    sys.exit("ERROR: FIRECRAWL_API_KEY and ANTHROPIC_API_KEY must be set as environment variables.")

firecrawl_app = FirecrawlApp(api_key=FIRECRAWL_API_KEY)
claude        = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

# ── Constants ────────────────────────────────────────────────────────────────
TRACKER_FILE = "Tracker.xlsx"
OUTPUT_FILE  = "index.html"
MODEL        = "claude-3-5-sonnet-20241022"
SCRAPE_DELAY = 2.0      # seconds between scrapes (respect Firecrawl rate limits)
MAX_CONTENT  = 6000     # chars sent to Claude per URL
MAX_COMBINED = 10000    # max combined chars for multi-URL rows

today        = datetime.date.today()
window_start = today - datetime.timedelta(days=10)
window_end   = today + datetime.timedelta(days=10)


# ── URL helpers ──────────────────────────────────────────────────────────────

def extract_clean_url(raw_value):
    """Return the first valid URL from a cell that may contain descriptive text."""
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
    """Collect up to 3 usable URLs per row. Falls back to URLs inside Custom Instruction."""
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


# ── Scraping & Claude helpers ────────────────────────────────────────────────

def scrape_url(url):
    """Scrape a URL with Firecrawl and return markdown text (or UNABLE_TO_ACCESS string)."""
    try:
        result = firecrawl_app.scrape_url(url, params={"formats": ["markdown"]})
        if isinstance(result, dict):
            text = result.get("markdown", "") or ""
        else:
            text = getattr(result, "markdown", "") or ""
        return text.strip()
    except Exception as exc:
        return f"UNABLE_TO_ACCESS: {str(exc)[:150]}"


def ask_claude(prompt):
    """Send a prompt to Claude 3.5 Sonnet and return the text response."""
    response = claude.messages.create(
        model=MODEL,
        max_tokens=1024,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.content[0].text.strip()


def parse_json_from_claude(raw):
    """Strip markdown fences if present, then json.loads."""
    if "```" in raw:
        raw = raw.split("```")[1]
        if raw.lower().startswith("json"):
            raw = raw[4:]
    return json.loads(raw.strip())


# ── Pass 1: News Monitor rows ────────────────────────────────────────────────

def run_news_monitor_pass(df):
    news_rows     = df[df["Category"] == "News Monitor"]
    product_rows  = df[df["Category"] != "News Monitor"]
    product_names = product_rows["Product Name"].dropna().tolist()

    results = []

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
Date window: {window_start} to {window_end} (±10 days)
News site: {site_name}  |  Region: {region}

PRODUCT LIST to match (case-insensitive, common variants allowed —
"Steam" matches "Steam Wallet Codes", "Free Fire" matches "Free Fire Diamond Pins", etc.):
{json.dumps(product_names, indent=2)}

NEWS CONTENT (first {MAX_CONTENT} chars):
{content[:MAX_CONTENT]}

Instructions:
1. Find every article/headline published within the date window that mentions any product above.
2. indirect_brand_news must be ≤25 words and end with the source name, e.g. "SoyaCincau: ...".
3. Return ONLY valid JSON — no markdown fences, no commentary.

{{
  "matches": [
    {{
      "matched_product":    "exact product name from the list",
      "event_region":       "region the news applies to",
      "event_name":         "3-5 word label",
      "indirect_brand_news":"≤25 word summary + source name",
      "announced_on":       "YYYY-MM-DD or blank",
      "duration":           "YYYY-MM-DD to YYYY-MM-DD or blank"
    }}
  ]
}}

If no matches, return: {{"matches": []}}"""

        try:
            data    = parse_json_from_claude(ask_claude(prompt))
            matches = data.get("matches", []) if isinstance(data, dict) else []
        except Exception as exc:
            print(f"    ⚠ JSON parse error for {site_name}: {exc}")
            matches = []

        results.append({"source_name": site_name, "region": region,
                        "scan_count": len(matches), "matches": matches})
        print(f"    → {len(matches)} match(es) found")

    return results


# ── Pass 2: Product rows ─────────────────────────────────────────────────────

def run_product_pass(df):
    product_rows = df[df["Category"] != "News Monitor"]
    results = []

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
            print(f"  [Product] {product_name} — no URL, skipping")
            base.update({"direct_brand_news": "No URL provided", "unable_to_access": True})
            results.append(base)
            continue

        print(f"  [Product] {product_name}  ({len(urls)} URL(s))...")

        scraped_parts = []
        for url in urls:
            content = scrape_url(url)
            scraped_parts.append(f"[Source: {url}]\n{content[:MAX_CONTENT]}")
            time.sleep(SCRAPE_DELAY)
        combined = "\n\n---\n\n".join(scraped_parts)[:MAX_COMBINED]

        if custom_instruction and custom_instruction != "nan":
            task_section = f"CUSTOM INSTRUCTION (follow exactly):\n{custom_instruction}"
        else:
            task_section = (
                "Generic logic — look for items ACTIVE OR ANNOUNCED within the date window:\n"
                "  • New product launches\n"
                "  • Bonus events (bonus credit, double rewards)\n"
                "  • Discounts / flash sales\n"
                "  • Discontinuations / region exits / expiry deadlines\n\n"
                "CONSOLIDATION RULE: If multiple sub-events fall under one umbrella campaign, "
                "keep them in ONE row with a summary in direct_brand_news. Only create "
                "separate findings for truly independent events."
            )

        prompt = f"""You are analysing product pages for the OffGamers weekly tracker.

Today: {today}
Date window: {window_start} to {window_end} (±10 days — look back AND forward)

Product: {product_name}  |  Category: {category}  |  Region: {region}

{task_section}

SCRAPED CONTENT:
{combined}

Return ONLY valid JSON — no markdown fences, no commentary:
{{
  "event_region":      "region the event applies to, or blank",
  "event_name":        "3-5 word event label, or blank if nothing found",
  "direct_brand_news": "concise summary (or 'Unable to access' if page failed)",
  "announced_on":      "YYYY-MM-DD or blank",
  "duration":          "YYYY-MM-DD to YYYY-MM-DD or blank",
  "unable_to_access":  false
}}"""

        try:
            data = parse_json_from_claude(ask_claude(prompt))
            if not isinstance(data, dict):
                raise ValueError("Expected JSON object")
        except Exception as exc:
            print(f"    ⚠ Parse error for {product_name}: {exc}")
            data = {"event_region": "", "event_name": "",
                    "direct_brand_news": f"Parse error: {str(exc)[:80]}",
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
        print(f"    → {base['event_name'] or '(no event)'}")

    return results


# ── HTML generation (Jinja2) ─────────────────────────────────────────────────

def row_css_class(r):
    name = (r.get("event_name")        or "").lower()
    news = (r.get("direct_brand_news") or "").lower()
    if r.get("unable_to_access") or "unable to access" in news:
        return "unable"
    if any(w in name for w in ["discontinu", "expir", "exit", "end", "remov", "clos"]):
        return "discontinue"
    if any(w in name for w in ["bonus", "discount", "sale", "promo", "reward", "offer"]):
        return "bonus"
    if r.get("event_name"):
        return "event"
    return "no-event"


HTML_TEMPLATE = Template(r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>OffGamers Weekly Tracker — {{ today }}</title>
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
thead th.sorted-asc::after{content:" ▲"}
thead th.sorted-desc::after{content:" ▼"}
tbody tr:nth-child(even){background:#fafbfd}
tbody tr:hover{background:#eef2ff}
td{padding:9px 12px;border-bottom:1px solid #f0f0f0;vertical-align:top}
.news-cell{max-width:320px;font-size:13px;line-height:1.5}
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
  <p>Run date: {{ today }} &nbsp;|&nbsp; Window: {{ window_start }} → {{ window_end }} &nbsp;|&nbsp; Powered by Firecrawl + Claude 3.5 Sonnet</p>
</header>
<div class="container">

  <div class="cards">
    <div class="card"><div class="val">{{ total_products }}</div><div class="lbl">Products Tracked</div></div>
    <div class="card"><div class="val">{{ events_found }}</div><div class="lbl">Events Found</div></div>
    <div class="card"><div class="val">{{ total_indirect }}</div><div class="lbl">Indirect News Items</div></div>
    <div class="card"><div class="val">{{ no_events }}</div><div class="lbl">No Events This Week</div></div>
    <div class="card"><div class="val">{{ unable_count }}</div><div class="lbl">Unable to Access</div></div>
  </div>

  <div class="section">
    <div class="section-header">Product Events</div>
    <div class="legend">
      <span><span class="dot" style="background:#4CAF50"></span>Event / Launch</span>
      <span><span class="dot" style="background:#FF9800"></span>Bonus / Discount</span>
      <span><span class="dot" style="background:#F44336"></span>Discontinuation / Expiry</span>
      <span><span class="dot" style="background:#9E9E9E"></span>Unable to Access</span>
      <span><span class="dot" style="background:#e0e0e0"></span>No Event</span>
    </div>
    <div class="filters">
      <label>Filter:</label>
      <input id="searchInput" type="text" placeholder="Search product..." oninput="filterProducts()">
      <select id="catFilter"    onchange="filterProducts()"><option value="">All Categories</option></select>
      <select id="regionFilter" onchange="filterProducts()"><option value="">All Regions</option></select>
      <select id="statusFilter" onchange="filterProducts()">
        <option value="">All Statuses</option>
        <option value="row-event">Has Event</option>
        <option value="row-bonus">Has Bonus/Discount</option>
        <option value="row-discontinue">Discontinuation</option>
        <option value="row-unable">Unable to Access</option>
        <option value="row-no-event">No Event</option>
      </select>
    </div>
    <table id="productTable">
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
          <td>{% if r.event_name %}{{ r.event_name }}{% else %}<span class="muted">—</span>{% endif %}</td>
          <td>{% if r.event_region %}{{ r.event_region }}{% else %}<span class="muted">—</span>{% endif %}</td>
          <td class="news-cell">{% if r.direct_brand_news %}{{ r.direct_brand_news }}{% else %}<span class="muted">—</span>{% endif %}</td>
          <td>{% if r.announced_on %}{{ r.announced_on }}{% else %}<span class="muted">—</span>{% endif %}</td>
          <td>{% if r.duration %}{{ r.duration }}{% else %}<span class="muted">—</span>{% endif %}</td>
          <td>{{ r.last_checked }}</td>
        </tr>
        {% endfor %}
      </tbody>
    </table>
  </div>

  <div class="section">
    <div class="section-header">Indirect Brand News (News Monitor sites)</div>
    <table id="newsTable">
      <thead><tr>
        <th onclick="sortTable('newsTbody',0)">Product Matched</th>
        <th onclick="sortTable('newsTbody',1)">Source</th>
        <th onclick="sortTable('newsTbody',2)">Region</th>
        <th onclick="sortTable('newsTbody',3)">Event Label</th>
        <th onclick="sortTable('newsTbody',4)">Indirect Brand News</th>
        <th onclick="sortTable('newsTbody',5)">Announced On</th>
        <th onclick="sortTable('newsTbody',6)">Duration</th>
      </tr></thead>
      <tbody id="newsTbody">
        {% set rowcount = namespace(value=0) %}
        {% for nm in news_matches %}
          {% for m in nm.matches %}
            {% set rowcount.value = rowcount.value + 1 %}
            <tr>
              <td>{{ m.matched_product }}</td>
              <td>{{ nm.source_name }}</td>
              <td>{{ nm.region }}</td>
              <td>{{ m.event_name or '—' }}</td>
              <td class="news-cell">{{ m.indirect_brand_news }}</td>
              <td>{{ m.announced_on or '—' }}</td>
              <td>{{ m.duration or '—' }}</td>
            </tr>
          {% endfor %}
        {% endfor %}
        {% if rowcount.value == 0 %}
          <tr><td colspan="7" class="muted center">No indirect brand news found this week.</td></tr>
        {% endif %}
      </tbody>
    </table>
  </div>

</div>
<script>
(function(){
  const rows = document.querySelectorAll('#productTbody tr');
  const cats = new Set(), regs = new Set();
  rows.forEach(r => { cats.add(r.dataset.category); regs.add(r.dataset.region); });
  const catSel = document.getElementById('catFilter');
  const regSel = document.getElementById('regionFilter');
  [...cats].filter(Boolean).sort().forEach(c => { const o=document.createElement('option'); o.value=c; o.textContent=c; catSel.appendChild(o); });
  [...regs].filter(Boolean).sort().forEach(r => { const o=document.createElement('option'); o.value=r; o.textContent=r; regSel.appendChild(o); });
})();
function filterProducts() {
  const search = document.getElementById('searchInput').value.toLowerCase();
  const cat = document.getElementById('catFilter').value;
  const reg = document.getElementById('regionFilter').value;
  const status = document.getElementById('statusFilter').value;
  document.querySelectorAll('#productTbody tr').forEach(row => {
    const name = row.cells[0].textContent.toLowerCase();
    const rowCls = [...row.classList].find(c => c.startsWith('row-')) || '';
    const show = (!search || name.includes(search))
              && (!cat    || row.dataset.category === cat)
              && (!reg    || row.dataset.region   === reg)
              && (!status || rowCls === status);
    row.style.display = show ? '' : 'none';
  });
}
const sortDir = {};
function sortTable(tbodyId, col) {
  const key = tbodyId + col;
  const asc = !sortDir[key];
  sortDir[key] = asc;
  const tbody = document.getElementById(tbodyId);
  const rows = [...tbody.querySelectorAll('tr')];
  rows.sort((a, b) => {
    const ta = (a.cells[col]?.textContent || '').trim();
    const tb = (b.cells[col]?.textContent || '').trim();
    return asc ? ta.localeCompare(tb) : tb.localeCompare(ta);
  });
  rows.forEach(r => tbody.appendChild(r));
  const tableEl = tbody.closest('table');
  tableEl.querySelectorAll('th').forEach((th, i) => {
    th.classList.remove('sorted-asc', 'sorted-desc');
    if (i === col) th.classList.add(asc ? 'sorted-asc' : 'sorted-desc');
  });
}
</script>
</body>
</html>""")


def generate_html(product_results, news_matches):
    # Annotate each product with its CSS class
    for r in product_results:
        r["css_class"] = row_css_class(r)

    return HTML_TEMPLATE.render(
        today          = today,
        window_start   = window_start,
        window_end     = window_end,
        product_results= product_results,
        news_matches   = news_matches,
        total_products = len(product_results),
        events_found   = sum(1 for r in product_results if r.get("event_name")),
        unable_count   = sum(1 for r in product_results if r.get("unable_to_access")),
        no_events      = sum(1 for r in product_results
                             if not r.get("event_name") and not r.get("unable_to_access")),
        total_indirect = sum(len(nm.get("matches", [])) for nm in news_matches),
    )


# ── Entry point ──────────────────────────────────────────────────────────────

def main():
    print(f"OffGamers Weekly Tracker — run date: {today}")
    print(f"Date window: {window_start} to {window_end}\n")

    df = pd.read_excel(TRACKER_FILE)
    print(f"Loaded {len(df)} rows from {TRACKER_FILE}\n")

    print("=== Pass 1: News Monitor ===")
    news_matches = run_news_monitor_pass(df)

    print("\n=== Pass 2: Products ===")
    product_results = run_product_pass(df)

    print("\nRendering index.html...")
    html = generate_html(product_results, news_matches)
    with open(OUTPUT_FILE, "w", encoding="utf-8") as fh:
        fh.write(html)

    events   = sum(1 for r in product_results if r.get("event_name"))
    unable   = sum(1 for r in product_results if r.get("unable_to_access"))
    indirect = sum(len(nm.get("matches", [])) for nm in news_matches)
    print(f"\nDone! → {OUTPUT_FILE}")
    print(f"  Products with events : {events}")
    print(f"  Unable to access     : {unable}")
    print(f"  Indirect news items  : {indirect}")


if __name__ == "__main__":
    main()
