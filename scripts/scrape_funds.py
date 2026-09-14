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
NAME_PATTERN = re.compile(r"#\s*\**PRU\**Link\**\s+([^\n]+)", re.I)


def parse_fund_page(text: str, url: str, debug: bool = False) -> dict:
    result = {"url": url}
    name_match = NAME_PATTERN.search(text)
    result["scraped_name"] = ("PRULink " + name_match.group(1).strip(" *")) if name_match else None

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

    matched, unmatched = 0, []
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

    from datetime import datetime, timezone, timedelta
    sgt = timezone(timedelta(hours=8))
    now = datetime.now(sgt)
    data["funds"]["updated_on"] = now.strftime("%d-%b-%Y")
    data["funds"]["updated_at"] = now.strftime("%d-%b-%Y %I:%M %p SGT")

    DATA_PATH.write_text(json.dumps(data))
    print(f"\nMatched {matched}/{len(data['funds']['funds'])} funds. data.json updated.")
    if unmatched:
        print(f"\nUnmatched ({len(unmatched)}) - name in data.json didn't match any scraped page title:", file=sys.stderr)
        for n in unmatched:
            print(f"  - {n}", file=sys.stderr)


if __name__ == "__main__":
    main()
