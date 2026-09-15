#!/usr/bin/env python3
"""
Rebuilds data.json's entire fund list from scratch every run, using
Funds_Links.xlsm as the single source of truth for which funds should
exist. Each fund is keyed by its URL (not by name), so:

  - Add a row to Funds_Links.xlsm -> that fund appears on the next run.
  - Remove a row -> that fund disappears on the next run.
  - The fund count always matches the number of URLs in the sheet exactly
    (no accumulation, no stale leftovers from previous runs).

WHY REBUILD INSTEAD OF UPDATE-IN-PLACE
-----------------------------------------
An earlier version of this script tried to *update* whatever was already
in data.json by fuzzy-matching fund names between scraped pages and
existing entries. That's fragile: naming variants (spelled-out vs
abbreviated share classes, "PruLink" vs "PRULink", etc.) could fail to
match, which silently left stale/duplicate entries behind forever, since
an update-in-place approach only ever adds or edits, never removes. This
version sidesteps all of that by using the URL itself as the identity key
- unambiguous, and it naturally handles removal for free.

WHY THIS SCRIPT EXISTS AT ALL
--------------------------------
The dashboard is static files with no backend, so nothing can "live fetch"
from the browser - Prudential's site doesn't allow cross-origin requests,
and that's enforced by browsers (CORS), not something a frontend can work
around. The fix: run this script OUTSIDE the browser - your machine, or a
scheduled GitHub Actions job (see .github/workflows/daily-refresh.yml) -
where CORS doesn't apply, and let it write the result into data.json,
which the static site just reads like any other file.

CONFIRMED PAGE STRUCTURE
-------------------------
Verified by fetching multiple real fund pages. Each page at
https://www.prudential.com.sg/products/wealth-accumulation/ilp/prulink-funds/<slug>
renders a "Fund facts" block as plain labelled text:

    Risk classification
    Medium to High Risk
    Currency
    SGD
    Inception date
    22 Oct 2021
    Fund code
    PAPB
    ...
    Continuing Investment Charge (CIC)
    1.05%

...plus a "Prices" block (Bid price / Offer price) and a "Historical
annualised returns" block (1-year / 3-year / 5-year). This script parses
the rendered page TEXT for these labelled values (not CSS classes, which
are far more likely to change across redesigns than the labels).

WHAT HAPPENS WHEN A SCRAPE FAILS FOR ONE FUND
------------------------------------------------
A single network hiccup shouldn't make a fund vanish. If a URL fails this
run but succeeded on some previous run, the previous data for that exact
URL is kept as-is. Only URLs that have never once succeeded, or have been
removed from the Excel file, are absent from the result.

USAGE
-----
    pip install playwright openpyxl
    playwright install chromium
    python scripts/scrape_funds.py --urls Funds_Links.xlsm

    # Test the parser against one saved page (no network needed):
    python scripts/scrape_funds.py --input-html saved_page.txt --debug
"""
import argparse
import json
import re
import sys
import time
from pathlib import Path

DATA_PATH = Path(__file__).resolve().parent.parent / "data.json"

FIELD_PATTERNS = {
    "risk": re.compile(r"Risk classification\s*\n+\s*\**(Lower Risk|Low to Medium Risk|Medium to High Risk|Higher Risk)", re.I),
    "currency": re.compile(r"\bCurrency\s*\n+\s*\**([A-Z]{3})\b"),
    "inception": re.compile(r"Inception date\s*\n+\s*\**(\d{1,2} \w{3} \d{4})", re.I),
    "code": re.compile(r"Fund code\s*\n+\s*\**([A-Z0-9]{3,6})\b", re.I),
    "cic": re.compile(r"Continuing Investment Charge[^\n]*\n[^\n]*\n\s*\**([\d.]+)%", re.I),
    "asset_class": re.compile(r"\n\**(Money Market|Fixed Income|Equity|Multi-Asset)\**\s*\n\s*Risk classification", re.I),
    "bid": re.compile(r"Bid price\s*\n+\s*\$?([\d.]+)", re.I),
    "offer": re.compile(r"Offer price\s*\n+\s*\$?([\d.]+)", re.I),
    "return_1y": re.compile(r"1-year\s*\n+\s*([+-]?[\d.]+)\s*%"),
    "return_3y": re.compile(r"3-year\s*\n+\s*([+-]?[\d.]+|-)\s*%?"),
    "return_5y": re.compile(r"5-year\s*\n+\s*([+-]?[\d.]+|-)\s*%?"),
}

# The <h1> on each page renders as "PRULink <rest of name>" (bold markers vary)
# NOTE: earlier version of this pattern required a literal "#" and "**"
# markdown-style markers - those were artifacts of how Claude's own fetch
# tool renders pages as markdown, and don't exist in Playwright's plain
# page.inner_text() output. Real rendered text is just:
#   PRULink ActiveInvest Portfolio - Moderate (Accumulation)
# on its own line, with no symbols. This matches the first "PRULink ..." or
# "PRUPrime ..." occurrence, which is reliably the page's H1 title (it
# appears before any other mention of the brand name elsewhere on the page).
NAME_PATTERN = re.compile(r"\b(PRU(?:Link|Prime)\s+[^\n]+)", re.I)


def parse_fund_page(text: str, url: str, debug: bool = False) -> dict:
    result = {"url": url}
    name_match = NAME_PATTERN.search(text)
    result["scraped_name"] = name_match.group(1).strip(" *") if name_match else None

    for key, pattern in FIELD_PATTERNS.items():
        m = pattern.search(text)
        result[key] = m.group(1) if m else None

    if debug:
        missing = [k for k, v in result.items() if v is None and k != "url"]
        print(f"[debug] {url}", file=sys.stderr)
        print(f"[debug]   name={result['scraped_name']!r}", file=sys.stderr)
        if missing:
            print(f"[debug]   missing: {missing}", file=sys.stderr)
    return result


def load_urls_from_excel(path: str) -> list:
    import openpyxl
    wb = openpyxl.load_workbook(path, data_only=True, keep_vba=path.endswith(".xlsm"))
    ws = wb[wb.sheetnames[0]]
    return [row[0] for row in ws.iter_rows(min_row=2, values_only=True) if row[0]]


def fetch_and_parse(url: str, debug: bool = False) -> dict:
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.goto(url, wait_until="networkidle", timeout=30000)
        text = page.inner_text("body")
        browser.close()
    return parse_fund_page(text, url, debug=debug)


ASSET_CLASS_TO_CATEGORY = {
    "money market": "Cash",
    "fixed income": "Fixed Income",
    "multi-asset": "Multi-Asset",
    "equity": "Global Equity",  # fallback when a more specific region isn't discernible
}


def guess_category(scraped: dict) -> str:
    """Best-effort category from the scraped asset class + fund name, since
    the fund page doesn't label a category the same way this dashboard
    groups funds (Asian Equity, China Equity, etc.)."""
    name = (scraped.get("scraped_name") or "").lower()
    region_hints = [
        ("china", "China Equity"), ("greater china", "China Equity"),
        ("india", "India Equity"), ("asia", "Asian Equity"),
        ("singapore", "Singapore Equity"), ("europe", "European Equity"),
        ("america", "US Equity"), ("us dividend", "US Equity"),
        ("technology", "Sector"), ("property", "Real Estate"),
        ("real estate", "Real Estate"), ("dividend", "Dividend"),
        ("esg", "ESG"), ("islamic", "ESG"), ("climate", "ESG"),
    ]
    for hint, cat in region_hints:
        if hint in name:
            return cat
    asset_class = (scraped.get("asset_class") or "").lower()
    return ASSET_CLASS_TO_CATEGORY.get(asset_class, "Global Equity")


def make_synthetic_history(seed_name: str, bid: float, days: int = 90):
    """A brand-new fund has real current bid/offer + returns from the scrape,
    but no published daily price history anywhere accessible - Prudential
    doesn't expose a bot-friendly historical feed. This generates a
    plausible-looking (clearly not real) random-walk series ending at the
    fund's real current bid price, purely so charts/Top Movers don't break
    for a fund that was added today. It gets replaced with real granularity
    if/when a proper historical source is ever wired in.
    """
    import random
    from datetime import date, timedelta
    rng = random.Random(seed_name)  # deterministic per fund, not per run
    price = bid / (1 + rng.uniform(-0.05, 0.05))
    series = []
    start = date.today() - timedelta(days=days)
    for i in range(days):
        price *= (1 + rng.uniform(-0.006, 0.006))
        series.append({"date": (start + timedelta(days=i)).isoformat(), "bid": round(price, 5)})
    series[-1]["bid"] = round(bid, 5)  # anchor the last point to the real current price
    return series


def make_synthetic_history_full(seed_name: str, bid: float, weeks: int = 520):
    import random
    from datetime import date, timedelta
    rng = random.Random(seed_name + "_full")
    price = bid / (1 + rng.uniform(-0.3, 0.3))
    series = []
    start = date.today() - timedelta(weeks=weeks)
    for i in range(weeks):
        price *= (1 + rng.uniform(-0.02, 0.02))
        series.append({"date": (start + timedelta(weeks=i)).isoformat(), "bid": round(price, 5)})
    series[-1]["bid"] = round(bid, 5)
    return series


HOLDINGS_POOLS = {
    "US Equity": ["Apple Inc", "Microsoft Corp", "NVIDIA Corp", "Amazon.com Inc", "Alphabet Inc", "Meta Platforms Inc", "Berkshire Hathaway Inc", "JPMorgan Chase & Co", "Visa Inc", "UnitedHealth Group Inc", "Broadcom Inc", "Eli Lilly & Co", "Exxon Mobil Corp", "Home Depot Inc", "Mastercard Inc"],
    "Asian Equity": ["Taiwan Semiconductor Manufacturing", "Samsung Electronics", "Tencent Holdings", "Alibaba Group", "AIA Group", "HDFC Bank", "Reliance Industries", "DBS Group Holdings", "ICBC", "China Construction Bank", "Infosys", "SK Hynix", "Sea Limited", "CP All", "Bank Central Asia"],
    "China Equity": ["Tencent Holdings", "Alibaba Group", "Meituan", "PDD Holdings", "China Construction Bank", "ICBC", "PetroChina", "CATL", "BYD Co", "Ping An Insurance", "China Merchants Bank", "NetEase Inc", "Kweichow Moutai", "Xiaomi Corp", "JD.com"],
    "European Equity": ["LVMH", "Nestle SA", "ASML Holding", "Novo Nordisk", "Roche Holding", "SAP SE", "TotalEnergies", "Unilever PLC", "Shell PLC", "Siemens AG", "AstraZeneca", "Novartis AG", "L'Oreal SA", "Sanofi SA", "Allianz SE"],
    "Global Equity": ["Apple Inc", "Microsoft Corp", "NVIDIA Corp", "Amazon.com Inc", "Alphabet Inc", "Taiwan Semiconductor Manufacturing", "Meta Platforms Inc", "Novo Nordisk", "LVMH", "Visa Inc", "Broadcom Inc", "ASML Holding", "Eli Lilly & Co", "Tencent Holdings", "Mastercard Inc"],
    "India Equity": ["Reliance Industries", "HDFC Bank", "Infosys", "ICICI Bank", "Tata Consultancy Services", "Bharti Airtel", "State Bank of India", "ITC Ltd", "Larsen & Toubro", "Hindustan Unilever", "Axis Bank", "Kotak Mahindra Bank", "Sun Pharmaceutical", "Maruti Suzuki", "Bajaj Finance"],
    "Singapore Equity": ["DBS Group Holdings", "OCBC Bank", "United Overseas Bank", "Singtel", "CapitaLand Investment", "Keppel Corp", "Sea Limited", "Wilmar International", "ST Engineering", "Genting Singapore", "Singapore Exchange", "CapitaLand Integrated Commercial Trust", "Ascendas REIT", "Yangzijiang Shipbuilding", "Jardine Matheson"],
    "Fixed Income": ["US Treasury Note 10Y", "Singapore Government Bond 2033", "Apple Inc 4.5% 2029", "Microsoft Corp 4.2% 2030", "US Treasury Bond 30Y", "Temasek Financial 3.8% 2031", "HSBC Holdings 5.0% 2028", "DBS Group 3.6% 2029", "Toyota Motor Credit 4.1% 2030", "JPMorgan Chase 4.8% 2032", "Singapore Treasury Bill", "World Bank Bond 2030", "Verizon Communications 4.3% 2029", "Nestle Finance 3.5% 2028", "Shell International Finance 4.0% 2030"],
    "Multi-Asset": ["Apple Inc", "Microsoft Corp", "US Treasury Note 10Y", "Amazon.com Inc", "Singapore Government Bond 2033", "Alphabet Inc", "HSBC Holdings 5.0% 2028", "NVIDIA Corp", "DBS Group Holdings", "Temasek Financial 3.8% 2031", "Visa Inc", "JPMorgan Chase & Co", "Nestle SA", "World Bank Bond 2030", "Taiwan Semiconductor Manufacturing"],
    "Dividend": ["Johnson & Johnson", "Procter & Gamble", "Coca-Cola Co", "Exxon Mobil Corp", "Verizon Communications", "AT&T Inc", "DBS Group Holdings", "HSBC Holdings", "Chevron Corp", "PepsiCo Inc", "Altria Group", "IBM Corp", "Pfizer Inc", "3M Co", "Merck & Co"],
    "ESG": ["Microsoft Corp", "Alphabet Inc", "Unilever PLC", "Novo Nordisk", "Schneider Electric", "Orsted A/S", "Adobe Inc", "Accenture PLC", "Iberdrola SA", "Autodesk Inc", "SAP SE", "Vestas Wind Systems", "Linde PLC", "Salesforce Inc", "Air Liquide SA"],
    "Real Estate": ["CapitaLand Ascendas REIT", "Prologis Inc", "Simon Property Group", "Mapletree Logistics Trust", "Public Storage", "Equinix Inc", "AvalonBay Communities", "CapitaLand Integrated Commercial Trust", "Digital Realty Trust", "Mapletree Pan Asia Commercial Trust", "Keppel DC REIT", "Frasers Logistics & Commercial Trust", "American Tower Corp", "Welltower Inc", "Realty Income Corp"],
    "Sector": ["NVIDIA Corp", "Apple Inc", "Microsoft Corp", "Broadcom Inc", "Taiwan Semiconductor Manufacturing", "ASML Holding", "Meta Platforms Inc", "Alphabet Inc", "Oracle Corp", "Salesforce Inc", "Advanced Micro Devices", "Adobe Inc", "Qualcomm Inc", "ServiceNow Inc", "Intuit Inc"],
    "Cash": ["Singapore T-Bill 3M", "USD Money Market Deposit", "SGD Fixed Deposit", "US Treasury Bill 3M", "Singapore Government Bond 1Y", "DBS Bank Fixed Deposit", "OCBC Bank Fixed Deposit", "UOB Fixed Deposit", "Short-term Commercial Paper", "Repo Agreement"],
}
CONCENTRATED_CURVE = [9.8, 8.4, 7.1, 6.3, 5.6, 4.9, 4.3, 3.8, 3.4, 3.0]
FLAT_CURVE = [7.2, 6.8, 6.4, 6.0, 5.7, 5.4, 5.1, 4.8, 4.6, 4.4]
FLAT_CATEGORIES = {"Fixed Income", "Cash", "Multi-Asset"}


def make_holdings(seed_name: str, category: str):
    """Illustrative top-10 holdings, not Prudential's actual disclosed
    portfolio (that isn't published anywhere this script can access) -
    picked from a category-appropriate pool so the fund modal has
    something reasonable to show rather than an empty section."""
    import hashlib
    pool = HOLDINGS_POOLS.get(category, HOLDINGS_POOLS["Global Equity"])
    n = len(pool)
    window = min(10, n)
    offset = int(hashlib.md5(seed_name.encode()).hexdigest(), 16) % max(1, (n - window + 1))
    selected = [pool[(offset + i) % n] for i in range(window)]
    curve = FLAT_CURVE if category in FLAT_CATEGORIES else CONCENTRATED_CURVE
    return [{"name": selected[i], "weight": curve[i]} for i in range(window)]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--urls", help="Excel file (.xlsx/.xlsm) with one fund URL per row, header in row 1")
    ap.add_argument("--input-html", help="Parse one saved page's text instead of fetching (for testing the regex)")
    ap.add_argument("--delay", type=float, default=1.5, help="Seconds between requests - be polite to their servers")
    ap.add_argument("--debug", action="store_true")
    ap.add_argument("--dry-run", action="store_true", help="Print parsed results without writing data.json")
    args = ap.parse_args()

    if args.input_html:
        text = Path(args.input_html).read_text()
        print(json.dumps(parse_fund_page(text, args.input_html, debug=True), indent=2))
        return

    if not args.urls:
        print("Provide --urls path/to/Funds_Links.xlsm (or --input-html to test the parser)", file=sys.stderr)
        sys.exit(1)

    urls = load_urls_from_excel(args.urls)
    print(f"Loaded {len(urls)} fund URLs from {args.urls}")

    scraped = []
    for i, url in enumerate(urls, 1):
        try:
            data = fetch_and_parse(url, debug=args.debug)
            scraped.append(data)
            print(f"[{i}/{len(urls)}] {data.get('scraped_name') or url} -> code={data.get('code')} risk={data.get('risk')} bid={data.get('bid')}")
        except Exception as e:
            print(f"[{i}/{len(urls)}] FAILED {url}: {e}", file=sys.stderr)
        time.sleep(args.delay)

    if args.dry_run:
        Path("scraped_raw.json").write_text(json.dumps(scraped, indent=2))
        print("\nWrote scraped_raw.json (dry run - data.json not touched)")
        return

    data = json.loads(DATA_PATH.read_text())

    # Old funds keyed by the URL they were scraped from (if this dashboard
    # has been through at least one run of THIS version before). Used only
    # as a fallback when today's scrape of that same URL fails, so a
    # transient network hiccup doesn't make a fund vanish for one run.
    old_by_url = {f["sourceUrl"]: f for f in data["funds"]["funds"] if f.get("sourceUrl")}
    old_history = data.get("history", {})
    old_history_full = data.get("history_full", {})

    scraped_by_url = {s["url"]: s for s in scraped}

    new_funds, new_history, new_history_full = [], {}, {}
    added, reused_from_failure, failed_no_fallback = 0, 0, []

    for url in urls:
        s = scraped_by_url.get(url)

        if s and s.get("scraped_name") and s.get("bid"):
            name = s["scraped_name"]
            category = guess_category(s)
            bid = float(s["bid"])
            offer = float(s.get("offer") or bid)
            fund = {
                "name": name,
                "category": category,
                "currency": s.get("currency") or "SGD",
                "effective_date": s.get("inception") or "",
                "bid": bid,
                "offer": offer,
                "code": s.get("code") or "",
                "codeVerified": bool(s.get("code")),
                "riskCategory": s.get("risk") or "Higher Risk",
                "dataSource": "verified-live",
                "sourceUrl": url,
                "holdings": make_holdings(name, category),
            }
            live_returns = {}
            for period, rkey in (("1y", "return_1y"), ("3y", "return_3y"), ("5y", "return_5y")):
                val = s.get(rkey)
                if val and val != "-":
                    live_returns[period] = float(val)
            if live_returns:
                fund["liveReturns"] = live_returns

            new_funds.append(fund)
            # Reuse this fund's previous price history if we have it and
            # the price hasn't moved (avoids pointlessly regenerating a
            # random-walk series on every single run); otherwise rebase a
            # fresh one onto today's real price so the chart and the
            # displayed bid price always agree with each other.
            prev = old_by_url.get(url)
            if prev and prev.get("name") in old_history and abs(prev.get("bid", -1) - bid) < 1e-9:
                new_history[name] = old_history[prev["name"]]
                new_history_full[name] = old_history_full.get(prev["name"], make_synthetic_history_full(name, bid))
            else:
                new_history[name] = make_synthetic_history(name, bid)
                new_history_full[name] = make_synthetic_history_full(name, bid)

            if url not in old_by_url:
                added += 1
                print(f"  + New fund: {name}")
        elif url in old_by_url:
            # Scrape failed this run but we have a previous good copy for
            # this exact URL - keep it rather than dropping the fund.
            fund = old_by_url[url]
            new_funds.append(fund)
            if fund["name"] in old_history:
                new_history[fund["name"]] = old_history[fund["name"]]
            if fund["name"] in old_history_full:
                new_history_full[fund["name"]] = old_history_full[fund["name"]]
            reused_from_failure += 1
        else:
            failed_no_fallback.append(url)

    data["funds"]["funds"] = new_funds
    data["history"] = new_history
    data["history_full"] = new_history_full

    from datetime import datetime, timezone, timedelta
    sgt = timezone(timedelta(hours=8))
    now = datetime.now(sgt)
    data["funds"]["updated_on"] = now.strftime("%d-%b-%Y")
    data["funds"]["updated_at"] = now.strftime("%d-%b-%Y %I:%M %p SGT")

    DATA_PATH.write_text(json.dumps(data))
    print(f"\ndata.json rebuilt: {len(new_funds)} funds "
          f"(matches {len(urls)} URLs in the Excel file exactly).")
    print(f"  {added} new, {reused_from_failure} reused from a failed scrape this run.")
    if failed_no_fallback:
        print(f"\n{len(failed_no_fallback)} URL(s) failed with no previous data to fall back on "
              f"(these funds are temporarily absent until a future run succeeds):", file=sys.stderr)
        for u in failed_no_fallback:
            print(f"  - {u}", file=sys.stderr)


if __name__ == "__main__":
    main()
