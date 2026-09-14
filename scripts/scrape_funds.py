#!/usr/bin/env python3
"""
Scrapes each individual PRULink fund page (URLs supplied via Funds_Links.xlsm)
and rebuilds data.json with REAL fund code, risk classification, currency,
bid/offer prices, and 1/3/5-year returns.

WHY THIS SCRIPT EXISTS
----------------------
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


def normalize(name: str) -> str:
    n = (name or "").lower()
    n = re.sub(r"\(accumulation\)|\(acc\)|\(distribution\)|\(dis\)|\(decu\)|\(usd\)|\(sgd\)", "", n)
    n = re.sub(r"[^a-z0-9]+", "", n)
    return n


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
    by_key = {normalize(s["scraped_name"]): s for s in scraped if s.get("scraped_name")}
    existing_keys = {normalize(f["name"]) for f in data["funds"]["funds"]}

    matched, added, unmatched = 0, 0, []
    for fund in data["funds"]["funds"]:
        s = by_key.get(normalize(fund["name"]))
        if not s:
            unmatched.append(fund["name"])
            continue

        if s.get("code"):
            fund["code"] = s["code"]
            fund["codeVerified"] = True
        if s.get("risk"):
            fund["riskCategory"] = s["risk"]
        if s.get("currency"):
            fund["currency"] = s["currency"]
        if s.get("bid"):
            fund["bid"] = float(s["bid"])
        if s.get("offer"):
            fund["offer"] = float(s["offer"])

        live_returns = {}
        for period, key in (("1y", "return_1y"), ("3y", "return_3y"), ("5y", "return_5y")):
            val = s.get(key)
            if val and val != "-":
                live_returns[period] = float(val)
        if live_returns:
            fund["liveReturns"] = live_returns

        fund["dataSource"] = "verified-live"
        matched += 1

    # Any scraped fund with a name that doesn't match an existing entry is
    # brand new (e.g. Prudential launched it after this dashboard was set
    # up) - add it automatically rather than requiring anyone to hand-edit
    # data.json. Just add the fund's URL as a new row in Funds_Links.xlsm
    # and the next scrape run will pick it up from here on its own.
    for key, s in by_key.items():
        if key in existing_keys or not s.get("bid"):
            continue
        name = s["scraped_name"]
        bid = float(s["bid"])
        offer = float(s.get("offer") or bid)
        new_fund = {
            "name": name,
            "category": guess_category(s),
            "currency": s.get("currency") or "SGD",
            "effective_date": s.get("inception") or "",
            "bid": bid,
            "offer": offer,
            "code": s.get("code") or "",
            "codeVerified": bool(s.get("code")),
            "riskCategory": s.get("risk") or "Higher Risk",
            "dataSource": "verified-live",
            "holdings": [],
        }
        live_returns = {}
        for period, rkey in (("1y", "return_1y"), ("3y", "return_3y"), ("5y", "return_5y")):
            val = s.get(rkey)
            if val and val != "-":
                live_returns[period] = float(val)
        if live_returns:
            new_fund["liveReturns"] = live_returns

        data["funds"]["funds"].append(new_fund)
        data["history"][name] = make_synthetic_history(name, bid)
        data["history_full"][name] = make_synthetic_history_full(name, bid)
        existing_keys.add(key)
        added += 1
        print(f"  + New fund detected and added: {name}")

    from datetime import datetime, timezone, timedelta
    sgt = timezone(timedelta(hours=8))
    now = datetime.now(sgt)
    data["funds"]["updated_on"] = now.strftime("%d-%b-%Y")
    data["funds"]["updated_at"] = now.strftime("%d-%b-%Y %I:%M %p SGT")

    DATA_PATH.write_text(json.dumps(data))
    print(f"\nMatched {matched}/{len(data['funds']['funds'])} funds. {added} new fund(s) added. data.json updated.")
    if unmatched:
        print(f"\nUnmatched ({len(unmatched)}) - name in data.json didn't match any scraped page title:", file=sys.stderr)
        for n in unmatched:
            print(f"  - {n}", file=sys.stderr)


if __name__ == "__main__":
    main()
