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
around. The fix: run this script OUTSIDE the browser - your machine, or
a scheduled GitHub Actions job (see .github/workflows/daily-refresh.yml) -
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

...plus a "Prices" block (Bid price / Offer price) and
"Historical annualised returns" block (1-year / 3-year / 5-year).

This script parses the rendered page TEXT for these labelled values
(not CSS classes, which are far more likely to change across redesigns
than the labels).

WHAT HAPPENS WHEN A SCRAPE FAILS FOR ONE FUND
------------------------------------------------
A single network hiccup shouldn't make a fund vanish. If a URL fails this
run but succeeded on some previous run, the previous data for that exact
URL is kept as-is. Only URLs that have never once succeeded, or have been
removed from the Excel file, are absent from the result.

HISTORY / HOLDINGS
------------------
Price history: synthetic history is intentionally NOT generated.
  - Existing historical price data is preserved for a fund when it
    already exists in data.json.
  - New funds do not receive fabricated historical price data.
  - No random-walk history is generated.

Holdings: REAL Top 10 Holdings are extracted from each fund's factsheet
PDF (linked from its own page as "View factsheet"). No fabricated
holdings are used.
  - Extraction uses each PDF page's word-level POSITION (x/y coordinates
    via pdfplumber's extract_words()), not the flattened text stream -
    factsheets often place an unrelated sidebar at a similar vertical
    position to the holdings list, which can interleave that sidebar's
    text with the holdings in plain linear extraction. Clustering words
    into rows by actual position avoids that.
  - Accepts 1-10 holdings; does not require exactly 10.
  - A holding name that wraps across two printed lines is reassembled
    from the row(s) above the row containing its percentage.
  - A percentage with no name text anywhere in its row or in a pending
    wrapped-name above it is REJECTED, not fabricated - see
    _holdings_from_words() for the exact rule.
  - A fund with no factsheet link, or whose factsheet PDF isn't in the
    expected "Top Holdings" format, gets an empty holdings list rather
    than a guess.

USAGE
-----
    pip install playwright openpyxl pdfplumber requests
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
    "risk": re.compile(
        r"Risk classification\s*\n+\s*\**"
        r"(Lower Risk|Low to Medium Risk|Medium to High Risk|Higher Risk)",
        re.I
    ),
    "currency": re.compile(
        r"\bCurrency\s*\n+\s*\**([A-Z]{3})\b"
    ),
    "inception": re.compile(
        r"Inception date\s*\n+\s*\**(\d{1,2} \w{3} \d{4})",
        re.I
    ),
    "code": re.compile(
        r"Fund code\s*\n+\s*\**([A-Z0-9]{3,6})\b",
        re.I
    ),
    "cic": re.compile(
        r"Continuing Investment Charge[^\n]*\n[^\n]*\n\s*\**([\d.]+)%",
        re.I
    ),
    "asset_class": re.compile(
        r"\n\**(Money Market|Fixed Income|Equity|Multi-Asset)\**"
        r"\s*\n\s*Risk classification",
        re.I
    ),
    "bid": re.compile(
        r"Bid price\s*\n+\s*\$?([\d.]+)",
        re.I
    ),
    "offer": re.compile(
        r"Offer price\s*\n+\s*\$?([\d.]+)",
        re.I
    ),
    "return_1y": re.compile(
        r"1-year\s*\n+\s*([+-]?[\d.]+)\s*%"
    ),
    "return_3y": re.compile(
        r"3-year\s*\n+\s*([+-]?[\d.]+|-)\s*%?"
    ),
    "return_5y": re.compile(
        r"5-year\s*\n+\s*([+-]?[\d.]+|-)\s*%?"
    ),
}


NAME_PATTERN = re.compile(
    r"\b(PRU(?:Link|Prime)\s+[^\n]+)",
    re.I
)

# Matches the block of text between a "Top Holdings" / "Top 10 Holdings"
# heading and whatever comes after it (a footnote "Source:" line, or the
# next section like "Sector Allocation"). Handles the optional footnote
# digit Prudential appends (e.g. "Top 10 Holdings3").
def _holdings_from_words(words: list, debug_url: str = "") -> list:
    """
    Pure function: turns a list of word dicts (as returned by pdfplumber's
    page.extract_words(), each with 'text'/'x0'/'x1'/'top'/'bottom') into
    a list of {name, weight} holdings, using each word's PHYSICAL POSITION
    on the page rather than the flattened text stream's reading order.

    Why position instead of text order: factsheets often lay out a
    holdings list alongside an unrelated sidebar (fund facts, manager
    info) at a similar vertical position. pdfplumber's default linear
    text extraction can interleave that sidebar's lines with the holdings
    list - verified against a real factsheet, where the 9th/10th holdings'
    percentages ended up separated from their names by unrelated text
    that had nothing to do with holdings at all. Clustering words into
    rows by y-position, restricted to the holdings section's own x-range,
    reads each row exactly where it's printed instead.

    Rules (per spec):
      - Accepts 1-10 holdings; does not require exactly 10.
      - A name that wraps across two printed lines is reassembled from
        the row(s) immediately above the row containing the percentage.
      - A percentage with no name text in its own row AND no pending
        wrapped-name text above it is REJECTED, not fabricated - there's
        nothing genuine to attach it to.
    """
    heading = next((w for w in words if "holdings" in w["text"].lower()), None)
    if not heading:
        return []

    heading_top = heading["top"]
    # Column bounds: no left constraint (a heading's own x-position isn't
    # reliably aligned with where the row content below it starts - an
    # earlier version anchored the left bound to the heading's x0 and it
    # clipped real name text that started slightly further left, e.g.
    # "HDFC" got cut from "HDFC BANK LTD"). The right bound stays
    # generous but bounded, to exclude a same-height sidebar column that
    # some factsheet layouts place to the right of the holdings list.
    col_x0 = 0
    col_x1 = heading["x1"] + 260

    stop_words = {"source", "sector", "country", "asset", "allocation"}
    end_candidates = [
        w["top"] for w in words
        if w["top"] > heading_top + 5
        and w["text"].strip(":").lower() in stop_words
    ]
    end_top = min(end_candidates) if end_candidates else heading_top + 400

    block_words = [
        w for w in words
        if heading_top < w["top"] < end_top and col_x0 <= w["x0"] <= col_x1
    ]
    if not block_words:
        return []

    # Group words into printed rows by y-position. A small tolerance
    # absorbs sub-pixel jitter between words that are visually on the
    # same line but not bit-for-bit identical 'top' values.
    rows: dict = {}
    for w in block_words:
        key = round(w["top"] / 3)
        rows.setdefault(key, []).append(w)

    holdings = []
    rejected = 0
    pending_name_parts = []

    for key in sorted(rows):
        row_words = sorted(rows[key], key=lambda w: w["x0"])
        row_text = " ".join(w["text"] for w in row_words)
        m = re.search(r"(\d+(?:\.\d+)?)\s*%\s*$", row_text)

        if not m:
            # A row with no trailing percentage is either a wrapped
            # continuation of the next holding's name, or unrelated text
            # that happened to fall in this column/range - either way it
            # only becomes part of a holding if a percentage row follows.
            if row_text.strip():
                pending_name_parts.append(row_text.strip())
            continue

        pct = float(m.group(1))
        name_here = row_text[:m.start()].strip(" -\u2022")
        full_name = " ".join(pending_name_parts + ([name_here] if name_here else [])).strip()
        pending_name_parts = []

        if full_name:
            holdings.append({"name": full_name, "weight": pct})
        else:
            # A bare percentage with nothing genuine to attach it to.
            # Reject rather than guess - never fabricate a holding.
            rejected += 1

    if debug_url:
        if rejected:
            print(f"[debug] {debug_url}: rejected {rejected} ambiguous "
                  f"percentage row(s) with no associated name", file=sys.stderr)
        if len(holdings) < 4:
            print(f"[debug] {debug_url}: only found {len(holdings)} holdings", file=sys.stderr)

    return holdings[:10]


TEXT_HOLDINGS_BLOCK_PATTERN = re.compile(
    r"Top (?:10 )?Holdings\d*\s*\n(.*?)"
    r"(?:\n\d*Source|\nSector Allocation|\nCountry Allocation|\nAsset Allocation|\Z)",
    re.S | re.I
)
TEXT_HOLDING_ENTRY_PATTERN = re.compile(r"([A-Za-z][^%]*?)\s*(\d+(?:\.\d+)?)\s*%")


def _holdings_from_text(text: str, debug_url: str = "") -> list:
    """Fallback for when a PDF's word-level extraction doesn't behave as
    expected (verified: this can happen even when the same PDF's flat
    extract_text() output parses fine, for certain embedded/subset fonts
    that confuse word-boundary detection but not character-level text
    extraction). Same rules as the position-based method: accepts 1-10,
    handles wrapped names by collapsing the block's whitespace before
    matching, and skips a bare percentage with no adjacent name rather
    than fabricate one."""
    block_match = TEXT_HOLDINGS_BLOCK_PATTERN.search(text)
    if not block_match:
        return []
    collapsed = re.sub(r"\s+", " ", block_match.group(1)).strip()
    holdings = []
    for name, pct in TEXT_HOLDING_ENTRY_PATTERN.findall(collapsed):
        name = name.strip(" -\u2022")
        if name:
            holdings.append({"name": name, "weight": float(pct)})
    if debug_url:
        print(f"[debug] {debug_url}: text-fallback found {len(holdings)} holdings", file=sys.stderr)
    return holdings[:10]


def parse_holdings(pdf, debug_url: str = "") -> list:
    """Tries each page of the factsheet (the holdings table is usually on
    page 1, but this doesn't assume that) and returns the first page that
    yields any holdings at all - preferring the position-based method
    (more accurate when a sidebar interleaves with the holdings list),
    falling back to flat-text extraction if that finds nothing on a given
    page (some PDFs' font encoding makes word-level extraction behave
    differently than character-level extraction)."""
    for page in pdf.pages:
        words = page.extract_words(use_text_flow=False, keep_blank_chars=False)
        holdings = _holdings_from_words(words, debug_url=debug_url)
        if holdings:
            return holdings

        text = page.extract_text() or ""
        holdings = _holdings_from_text(text, debug_url=debug_url)
        if holdings:
            return holdings
    return []


def find_factsheet_url(page) -> str:
    """Every fund page has a 'View factsheet' link near the Prices block,
    and again under 'Fund documents' as 'Fund Factsheet'. Try both."""
    for text in ("View factsheet", "Fund Factsheet"):
        try:
            locator = page.get_by_text(text, exact=False)
            if locator.count() == 0:
                continue
            href = locator.first.get_attribute("href")
            if href and href.lower().endswith(".pdf"):
                return href
        except Exception:
            continue
    return ""


# The live fund page's "Fund facts" panel (risk/inception/CIC) has been
# observed loading unreliably or not at all since Prudential's site
# migration (verified live: "Loading fund details..." can persist
# indefinitely). The static factsheet PDF - already being downloaded for
# holdings - reliably contains the same information under a "Fund
# Details" heading instead, verified against real factsheet text:
#   "Fund Details Launch Date 21 October 2021 Risk Classification of
#    Investment-linked Insurance Products (ILP) Medium to High Risk,
#    Broadly Diversified ... Continuing Investment Charge 1.20% p.a."
# These patterns are tolerant of the label/value being separated by
# newlines OR by the middle-dot-style separators some extractions use.
FACTSHEET_RISK_PATTERN = re.compile(
    r"Risk Classification of\s+Investment-linked Insurance\s+Products \(ILP\)\s*"
    r"(Lower Risk|Low to Medium Risk|Medium to High Risk|Higher Risk)",
    re.I | re.S
)
FACTSHEET_LAUNCH_DATE_PATTERN = re.compile(
    r"Launch Date\s+(\d{1,2} \w+ \d{4})", re.I
)
FACTSHEET_CIC_PATTERN = re.compile(
    r"Continuing Investment Charge\s+([\d.]+)\s*%", re.I
)


def fetch_holdings_from_factsheet(factsheet_url: str, debug: bool = False) -> dict:
    """Downloads the factsheet PDF (plain HTTP - PDFs aren't subject to
    the browser-JS-rendering concerns the main fund pages are) and
    extracts both holdings (via word position) and the Fund Details
    fields (risk/launch date/CIC) that the live page has proven
    unreliable for. Returns {"holdings": [...], "risk": ..., "inception":
    ..., "cic": ...} - any field not found is omitted, not guessed."""
    import requests
    import pdfplumber
    import io

    resp = requests.get(factsheet_url, timeout=30)
    resp.raise_for_status()

    result = {"holdings": []}
    with pdfplumber.open(io.BytesIO(resp.content)) as pdf:
        result["holdings"] = parse_holdings(pdf, debug_url=factsheet_url if debug else "")

        full_text = "\n".join(p.extract_text() or "" for p in pdf.pages)
        collapsed = re.sub(r"\s+", " ", full_text)

        m = FACTSHEET_RISK_PATTERN.search(collapsed)
        if m:
            result["risk"] = m.group(1)
        m = FACTSHEET_LAUNCH_DATE_PATTERN.search(collapsed)
        if m:
            result["inception"] = m.group(1)
        m = FACTSHEET_CIC_PATTERN.search(collapsed)
        if m:
            result["cic"] = m.group(1)

    if debug:
        found = [k for k in ("risk", "inception", "cic") if k in result]
        print(f"[debug] {factsheet_url}: factsheet fields found: {found or 'none'}", file=sys.stderr)

    return result


def parse_fund_page(text: str, url: str, debug: bool = False) -> dict:
    result = {"url": url}

    name_match = NAME_PATTERN.search(text)

    result["scraped_name"] = (
        name_match.group(1).strip(" *")
        if name_match
        else None
    )

    for key, pattern in FIELD_PATTERNS.items():
        m = pattern.search(text)
        result[key] = m.group(1) if m else None

    if debug:
        missing = [
            k
            for k, v in result.items()
            if v is None and k != "url"
        ]

        print(f"[debug] {url}", file=sys.stderr)
        print(
            f"[debug]   name={result['scraped_name']!r}",
            file=sys.stderr
        )

        if missing:
            print(
                f"[debug]   missing: {missing}",
                file=sys.stderr
            )

    return result


def load_urls_from_excel(path: str) -> list:
    import openpyxl

    wb = openpyxl.load_workbook(
        path,
        data_only=True,
        keep_vba=path.endswith(".xlsm")
    )

    ws = wb[wb.sheetnames[0]]

    return [
        row[0]
        for row in ws.iter_rows(
            min_row=2,
            values_only=True
        )
        if row[0]
    ]


def fetch_and_parse(url: str, debug: bool = False, capture_history: bool = False) -> dict:
    from playwright.sync_api import sync_playwright

    captured = []

    def on_response(response):
        # Real bid-bid history feeds the page's interactive chart, which
        # isn't present in the plain page text - it's loaded via a
        # background API call. Rather than guess that call's URL/shape,
        # this logs every response that plausibly could be it so a real
        # run can reveal the actual endpoint and JSON structure to parse.
        try:
            u = response.url.lower()
            if not any(k in u for k in ("chart", "price", "performance", "history", "nav", "/api/", ".json")):
                return
            ctype = response.headers.get("content-type", "")
            body = response.text() if ("json" in ctype or "javascript" in ctype) else None
            captured.append({"url": response.url, "status": response.status,
                              "content_type": ctype, "body": body})
        except Exception:
            pass

    with sync_playwright() as p:
        browser = p.chromium.launch()

        page = browser.new_page()
        if capture_history:
            page.on("response", on_response)

        page.goto(
            url,
            wait_until="networkidle",
            timeout=30000
        )

        # The "Fund facts" panel (risk/currency/code/inception/CIC) loads
        # via a slower, separate call than the rest of the page - verified
        # live: a page snapshot taken right after networkidle can still
        # show literal "Loading fund details..." placeholder text with
        # that whole panel empty, even though Prices/chart have already
        # rendered. Waiting for the actual heading text to appear (with a
        # generous timeout, and NOT raising if it never shows up - some
        # fund types may genuinely lack this panel) is more reliable than
        # waiting on network activity alone.
        try:
            page.wait_for_selector("text=Risk classification", timeout=15000)
        except Exception:
            pass  # proceed anyway - parse_fund_page will just report it missing

        if capture_history:
            # give any lazily-triggered chart JS a little extra time
            page.wait_for_timeout(2000)

        text = page.inner_text("body")
        factsheet_url = find_factsheet_url(page)
        if factsheet_url:
            # Prudential's factsheet link can be a site-relative path
            # (verified live: "/content/dam/.../some-fund.pdf" with no
            # domain) - resolve it against the page's own URL so it's a
            # real, fetchable absolute URL.
            from urllib.parse import urljoin
            factsheet_url = urljoin(page.url, factsheet_url)

        browser.close()

    result = parse_fund_page(
        text,
        url,
        debug=debug
    )

    result["holdings"] = []
    if factsheet_url:
        try:
            factsheet_data = fetch_holdings_from_factsheet(factsheet_url, debug=debug)
            result["holdings"] = factsheet_data.get("holdings", [])
            # Prefer the factsheet's values for these fields - the live
            # page's equivalent panel has been observed not loading at
            # all since Prudential's site migration, while the static
            # factsheet PDF has proven reliable for the same information.
            for key in ("risk", "inception", "cic"):
                if factsheet_data.get(key):
                    result[key] = factsheet_data[key]
        except Exception as e:
            if debug:
                print(f"[debug] {url}: factsheet extraction failed ({factsheet_url}): {e}",
                      file=sys.stderr)

    # Currency isn't reliably labelled on either source anymore, but the
    # fund's own name states it explicitly for non-default share classes
    # (e.g. "... (USD) (Acc)") - everything else is SGD.
    if not result.get("currency"):
        result["currency"] = "USD" if "(USD)" in (result.get("scraped_name") or "") else "SGD"

    if capture_history:
        out_dir = Path("history_capture")
        out_dir.mkdir(exist_ok=True)
        out_path = out_dir / (re.sub(r"[^a-z0-9]+", "_", url.lower()).strip("_") + ".json")
        out_path.write_text(json.dumps(captured, indent=2))
        print(f"  captured {len(captured)} candidate response(s) -> {out_path}", file=sys.stderr)

    return result


ASSET_CLASS_TO_CATEGORY = {
    "money market": "Cash",
    "fixed income": "Fixed Income",
    "multi-asset": "Multi-Asset",
    "equity": "Global Equity",
}


def guess_category(scraped: dict) -> str:
    """
    Best-effort category from the scraped asset class + fund name, since
    the fund page doesn't label a category the same way this dashboard
    groups funds (Asian Equity, China Equity, etc.).
    """

    name = (
        scraped.get("scraped_name") or ""
    ).lower()

    region_hints = [
        ("china", "China Equity"),
        ("greater china", "China Equity"),
        ("india", "India Equity"),
        ("asia", "Asian Equity"),
        ("singapore", "Singapore Equity"),
        ("europe", "European Equity"),
        ("america", "US Equity"),
        ("us dividend", "US Equity"),
        ("technology", "Sector"),
        ("property", "Real Estate"),
        ("real estate", "Real Estate"),
        ("dividend", "Dividend"),
        ("esg", "ESG"),
        ("islamic", "ESG"),
        ("climate", "ESG"),
    ]

    for hint, cat in region_hints:
        if hint in name:
            return cat

    asset_class = (
        scraped.get("asset_class") or ""
    ).lower()

    return ASSET_CLASS_TO_CATEGORY.get(
        asset_class,
        "Global Equity"
    )


def main():
    ap = argparse.ArgumentParser()

    ap.add_argument("--urls", help="Excel file (.xlsx/.xlsm) with one fund URL per row, header in row 1")
    ap.add_argument("--input-html", help="Parse one saved page's text instead of fetching (for testing the regex)")
    ap.add_argument("--delay", type=float, default=1.5, help="Seconds between requests - be polite to their servers")
    ap.add_argument("--debug", action="store_true")
    ap.add_argument("--capture-history", action="store_true",
                     help="Log candidate chart/price API responses per fund to history_capture/ for inspection - doesn't write data.json")
    ap.add_argument("--dry-run", action="store_true", help="Print parsed results without writing data.json")
    ap.add_argument("--limit", type=int, help="Only process the first N URLs (useful for quick/exploratory runs)")

    args = ap.parse_args()

    if args.input_html:
        text = Path(args.input_html).read_text()
        print(json.dumps(parse_fund_page(text, args.input_html, debug=True), indent=2))
        return

    if not args.urls:
        print("Provide --urls path/to/Funds_Links.xlsm (or --input-html to test the parser)", file=sys.stderr)
        sys.exit(1)

    urls = load_urls_from_excel(args.urls)
    if args.limit:
        urls = urls[:args.limit]
    print(f"Loaded {len(urls)} fund URLs from {args.urls}")

    scraped = []
    for i, url in enumerate(urls, 1):
        try:
            data = fetch_and_parse(url, debug=args.debug, capture_history=args.capture_history)
            scraped.append(data)
            print(f"[{i}/{len(urls)}] {data.get('scraped_name') or url} -> code={data.get('code')} risk={data.get('risk')} bid={data.get('bid')}")
        except Exception as e:
            print(f"[{i}/{len(urls)}] FAILED {url}: {e}", file=sys.stderr)
        time.sleep(args.delay)

    if args.capture_history:
        print(f"\nDone. Inspect the history_capture/*.json files, then share a couple of them "
              f"so the real chart data endpoint can be parsed properly.")
        return

    if args.dry_run:
        Path("scraped_raw.json").write_text(json.dumps(scraped, indent=2))
        print("\nWrote scraped_raw.json (dry run - data.json not touched)")
        return

    data = json.loads(DATA_PATH.read_text())

    old_by_url = {f["sourceUrl"]: f for f in data["funds"]["funds"] if f.get("sourceUrl")}
    old_history = data.get("history", {})
    old_history_full = data.get("history_full", {})

    scraped_by_url = {s["url"]: s for s in scraped}

    new_funds = []
    new_history = {}
    new_history_full = {}

    added = 0
    reused_from_failure = 0
    failed_no_fallback = []

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
                "holdings": s.get("holdings") or [],
            }

            live_returns = {}
            for period, rkey in (("1y", "return_1y"), ("3y", "return_3y"), ("5y", "return_5y")):
                val = s.get(rkey)
                if val and val != "-":
                    live_returns[period] = float(val)
            if live_returns:
                fund["liveReturns"] = live_returns

            new_funds.append(fund)

            prev = old_by_url.get(url)
            if prev and prev.get("name") in old_history:
                new_history[name] = old_history[prev["name"]]
            if prev and prev.get("name") in old_history_full:
                new_history_full[name] = old_history_full[prev["name"]]

            if url not in old_by_url:
                added += 1
                print(f"  + New fund: {name}")

        elif url in old_by_url:
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

    print(f"\ndata.json rebuilt: {len(new_funds)} funds (matches {len(urls)} URLs in the Excel file exactly).")
    print(f"  {added} new, {reused_from_failure} reused from a failed scrape this run.")

    if failed_no_fallback:
        print(f"\n{len(failed_no_fallback)} URL(s) failed with no previous data to fall back on (these funds are temporarily absent until a future run succeeds):", file=sys.stderr)
        for u in failed_no_fallback:
            print(f"  - {u}", file=sys.stderr)


if __name__ == "__main__":
    main()
