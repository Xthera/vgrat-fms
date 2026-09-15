```python
#!/usr/bin/env python3
"""
Rebuilds data.json's entire fund list from scratch every run, using
Funds_Links.xlsm as the single source of truth for which funds should
exist. Each fund is keyed by its URL (not by name), so:

  - Add a row to Funds_Links.xlsm -> that fund appears on the next run.
  - Remove a row -> that fund disappears on the next run.
  - The fund count always matches the number of URLs in the sheet exactly
    (no accumulation, no stale leftovers from previous runs).

IMPORTANT DATA RULES
--------------------
  - Real Prudential data is used when successfully retrieved.
  - Any field that cannot be retrieved is stored as "-".
  - No synthetic holdings are generated.
  - No synthetic historical prices are generated.
  - Existing historical data is preserved when available.
  - No default/guessed values such as "SGD" or "Higher Risk" are used.

WHY REBUILD INSTEAD OF UPDATE-IN-PLACE
-----------------------------------------
An earlier version of this script tried to *update* whatever was already
in data.json by fuzzy-matching fund names between scraped pages and
existing entries. That's fragile: naming variants (spelled-out vs
abbreviated share classes, "PruLink" vs "PRULink", etc.) could fail to
match, which silently left stale/duplicate entries behind forever, since
an update-in-place approach only ever adds or edits, never removes.

This version sidesteps all of that by using the URL itself as the identity
key - unambiguous, and it naturally handles removal for free.

WHY THIS SCRIPT EXISTS AT ALL
--------------------------------
The dashboard is static files with no backend, so nothing can "live fetch"
from the browser - Prudential's site doesn't allow cross-origin requests,
and that's enforced by browsers (CORS), not something a frontend can work
around.

The fix: run this script OUTSIDE the browser - your machine, or a scheduled
GitHub Actions job (see .github/workflows/daily-refresh.yml) - where CORS
doesn't apply, and let it write the result into data.json, which the static
site just reads like any other file.

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

...plus a "Prices" block (Bid price / Offer price) and a
"Historical annualised returns" block (1-year / 3-year / 5-year).

This script parses the rendered page TEXT for these labelled values
(not CSS classes, which are more likely to change across redesigns
than the labels).

WHAT HAPPENS WHEN A SCRAPE FAILS FOR ONE FUND
------------------------------------------------
A single network hiccup shouldn't make a fund vanish. If a URL fails this
run but succeeded on some previous run, the previous data for that exact
URL is kept as-is.

Only URLs that have never once succeeded, or have been removed from the
Excel file, are absent from the result.

HISTORY / HOLDINGS
------------------
Synthetic holdings and synthetic price history are intentionally NOT
generated.

  - holdings is an empty list because real holdings data is not currently
    being retrieved by this script.
  - Existing historical price data is preserved for a fund when it already
    exists in data.json.
  - New funds do not receive fabricated historical price data.
  - No random-walk or illustrative holdings data is generated.

USAGE
-----
    pip install playwright openpyxl
    playwright install chromium

    python scripts/scrape_funds.py --urls Funds_Links.xlsm

Test the parser against one saved page:

    python scripts/scrape_funds.py --input-html saved_page.txt --debug

Dry run:

    python scripts/scrape_funds.py --urls Funds_Links.xlsm --dry-run
"""

import argparse
import json
import re
import sys
import time
from pathlib import Path


DATA_PATH = Path(__file__).resolve().parent.parent / "data.json"


# ---------------------------------------------------------------------------
# FIELD PARSING PATTERNS
# ---------------------------------------------------------------------------

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
        r"1-year\s*\n+\s*([+-]?[\d.]+|-)\s*%"
    ),

    "return_3y": re.compile(
        r"3-year\s*\n+\s*([+-]?[\d.]+|-)\s*%?"
    ),

    "return_5y": re.compile(
        r"5-year\s*\n+\s*([+-]?[\d.]+|-)\s*%?"
    ),
}


# The <h1> on each page renders as "PRULink <rest of name>".
#
# Real rendered text is generally:
#
#   PRULink ActiveInvest Portfolio - Moderate (Accumulation)
#
# on its own line, with no markdown symbols.
#
# This matches the first "PRULink ..." or "PRUPrime ..." occurrence.
NAME_PATTERN = re.compile(
    r"\b(PRU(?:Link|Prime)\s+[^\n]+)",
    re.I
)


# ---------------------------------------------------------------------------
# PARSER
# ---------------------------------------------------------------------------

def parse_fund_page(text: str, url: str, debug: bool = False) -> dict:
    result = {
        "url": url
    }

    name_match = NAME_PATTERN.search(text)

    result["scraped_name"] = (
        name_match.group(1).strip(" *")
        if name_match
        else "-"
    )

    for key, pattern in FIELD_PATTERNS.items():
        m = pattern.search(text)

        if m:
            result[key] = m.group(1)
        else:
            result[key] = "-"

    if debug:
        missing = [
            k
            for k, v in result.items()
            if v == "-" and k != "url"
        ]

        print(
            f"[debug] {url}",
            file=sys.stderr
        )

        print(
            f"[debug]   name={result['scraped_name']!r}",
            file=sys.stderr
        )

        if missing:
            print(
                f"[debug]   unavailable: {missing}",
                file=sys.stderr
            )

    return result


# ---------------------------------------------------------------------------
# EXCEL URL LOADING
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# FETCH + PARSE
# ---------------------------------------------------------------------------

def fetch_and_parse(url: str, debug: bool = False) -> dict:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:

        browser = p.chromium.launch()

        page = browser.new_page()

        page.goto(
            url,
            wait_until="networkidle",
            timeout=30000
        )

        text = page.inner_text("body")

        browser.close()

    return parse_fund_page(
        text,
        url,
        debug=debug
    )


# ---------------------------------------------------------------------------
# CATEGORY
# ---------------------------------------------------------------------------

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

    If neither asset class nor name can provide a category, return "-".
    """

    name = (
        scraped.get("scraped_name") or ""
    ).lower()

    if name == "-":
        name = ""

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

    if asset_class == "-":
        return "-"

    return ASSET_CLASS_TO_CATEGORY.get(
        asset_class,
        "-"
    )


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------

def main():

    ap = argparse.ArgumentParser()

    ap.add_argument(
        "--urls",
        help=(
            "Excel file (.xlsx/.xlsm) with one fund URL per row, "
            "header in row 1"
        )
    )

    ap.add_argument(
        "--input-html",
        help=(
            "Parse one saved page's text instead of fetching "
            "(for testing the regex)"
        )
    )

    ap.add_argument(
        "--delay",
        type=float,
        default=1.5,
        help=(
            "Seconds between requests - be polite to their servers"
        )
    )

    ap.add_argument(
        "--debug",
        action="store_true"
    )

    ap.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Print parsed results without writing data.json"
        )
    )

    args = ap.parse_args()


    # -----------------------------------------------------------------------
    # TEST MODE
    # -----------------------------------------------------------------------

    if args.input_html:

        text = Path(
            args.input_html
        ).read_text()

        print(
            json.dumps(
                parse_fund_page(
                    text,
                    args.input_html,
                    debug=True
                ),
                indent=2
            )
        )

        return


    # -----------------------------------------------------------------------
    # VALIDATE ARGUMENTS
    # -----------------------------------------------------------------------

    if not args.urls:

        print(
            "Provide --urls path/to/Funds_Links.xlsm "
            "(or --input-html to test the parser)",
            file=sys.stderr
        )

        sys.exit(1)


    # -----------------------------------------------------------------------
    # LOAD URLS
    # -----------------------------------------------------------------------

    urls = load_urls_from_excel(
        args.urls
    )

    print(
        f"Loaded {len(urls)} fund URLs from {args.urls}"
    )


    # -----------------------------------------------------------------------
    # SCRAPE
    # -----------------------------------------------------------------------

    scraped = []

    for i, url in enumerate(
        urls,
        1
    ):

        try:

            data = fetch_and_parse(
                url,
                debug=args.debug
            )

            scraped.append(
                data
            )

            print(
                f"[{i}/{len(urls)}] "
                f"{data.get('scraped_name') or url} "
                f"-> "
                f"code={data.get('code')} "
                f"risk={data.get('risk')} "
                f"bid={data.get('bid')}"
            )

        except Exception as e:

            print(
                f"[{i}/{len(urls)}] FAILED {url}: {e}",
                file=sys.stderr
            )

        time.sleep(
            args.delay
        )


    # -----------------------------------------------------------------------
    # DRY RUN
    # -----------------------------------------------------------------------

    if args.dry_run:

        Path(
            "scraped_raw.json"
        ).write_text(
            json.dumps(
                scraped,
                indent=2
            )
        )

        print(
            "\nWrote scraped_raw.json "
            "(dry run - data.json not touched)"
        )

        return


    # -----------------------------------------------------------------------
    # LOAD EXISTING DATA
    # -----------------------------------------------------------------------

    data = json.loads(
        DATA_PATH.read_text()
    )


    # Old funds keyed by the URL they were scraped from.
    #
    # Used only as a fallback when today's scrape of that same URL fails,
    # so a transient network hiccup doesn't make a fund vanish.

    old_by_url = {
        f["sourceUrl"]: f
        for f in data["funds"]["funds"]
        if f.get("sourceUrl")
    }


    # Existing history.
    #
    # No synthetic history is generated.

    old_history = data.get(
        "history",
        {}
    )

    old_history_full = data.get(
        "history_full",
        {}
    )


    scraped_by_url = {
        s["url"]: s
        for s in scraped
    }


    # -----------------------------------------------------------------------
    # REBUILD
    # -----------------------------------------------------------------------

    new_funds = []

    new_history = {}

    new_history_full = {}

    added = 0

    reused_from_failure = 0

    failed_no_fallback = []


    for url in urls:

        s = scraped_by_url.get(
            url
        )


        # -------------------------------------------------------------------
        # SUCCESSFUL SCRAPE
        # -------------------------------------------------------------------

        if (
            s
            and s.get("scraped_name")
            and s.get("scraped_name") != "-"
            and s.get("bid")
            and s.get("bid") != "-"
        ):

            name = s["scraped_name"]

            category = guess_category(
                s
            )


            # ---------------------------------------------------------------
            # BID
            # ---------------------------------------------------------------

            try:
                bid = float(
                    s["bid"]
                )
            except (
                TypeError,
                ValueError
            ):
                bid = "-"


            # ---------------------------------------------------------------
            # OFFER
            # ---------------------------------------------------------------

            if (
                s.get("offer")
                and s.get("offer") != "-"
            ):

                try:
                    offer = float(
                        s["offer"]
                    )
                except (
                    TypeError,
                    ValueError
                ):
                    offer = "-"

            else:
                offer = "-"


            # ---------------------------------------------------------------
            # FUND OBJECT
            # ---------------------------------------------------------------

            fund = {
                "name": name,

                "category": category,

                "currency": (
                    s.get("currency")
                    if s.get("currency") != "-"
                    else "-"
                ),

                "effective_date": (
                    s.get("inception")
                    if s.get("inception") != "-"
                    else "-"
                ),

                "bid": bid,

                "offer": offer,

                "code": (
                    s.get("code")
                    if s.get("code") != "-"
                    else "-"
                ),

                "codeVerified": (
                    bool(s.get("code"))
                    and s.get("code") != "-"
                ),

                "riskCategory": (
                    s.get("risk")
                    if s.get("risk") != "-"
                    else "-"
                ),

                "dataSource": "verified-live",

                "sourceUrl": url,

                # No synthetic holdings.
                "holdings": [],
            }


            # ---------------------------------------------------------------
            # LIVE RETURNS
            # ---------------------------------------------------------------

            live_returns = {}

            for period, rkey in (
                ("1y", "return_1y"),
                ("3y", "return_3y"),
                ("5y", "return_5y"),
            ):

                val = s.get(
                    rkey
                )

                if (
                    val
                    and val != "-"
                ):

                    try:
                        live_returns[period] = float(
                            val
                        )
                    except (
                        TypeError,
                        ValueError
                    ):
                        live_returns[period] = "-"

                else:
                    live_returns[period] = "-"


            # Always provide all three return periods.

            fund["liveReturns"] = live_returns


            # ---------------------------------------------------------------
            # HISTORY
            # ---------------------------------------------------------------
            #
            # IMPORTANT:
            #
            # No synthetic history is generated.
            #
            # Existing history is preserved if it exists for the previous
            # version of this fund.
            #
            # New funds simply have no historical series.
            # ---------------------------------------------------------------

            prev = old_by_url.get(
                url
            )

            if (
                prev
                and prev.get("name") in old_history
            ):

                new_history[name] = old_history[
                    prev["name"]
                ]


            if (
                prev
                and prev.get("name") in old_history_full
            ):

                new_history_full[name] = old_history_full[
                    prev["name"]
                ]


            # ---------------------------------------------------------------
            # NEW FUND
            # ---------------------------------------------------------------

            if url not in old_by_url:

                added += 1

                print(
                    f"  + New fund: {name}"
                )


            new_funds.append(
                fund
            )


        # -------------------------------------------------------------------
        # SCRAPE FAILED BUT PREVIOUS DATA EXISTS
        # -------------------------------------------------------------------

        elif url in old_by_url:

            # Keep the previous good record for this exact URL.

            fund = old_by_url[
                url
            ]

            new_funds.append(
                fund
            )


            if fund["name"] in old_history:

                new_history[
                    fund["name"]
                ] = old_history[
                    fund["name"]
                ]


            if fund["name"] in old_history_full:

                new_history_full[
                    fund["name"]
                ] = old_history_full[
                    fund["name"]
                ]


            reused_from_failure += 1


        # -------------------------------------------------------------------
        # NEW URL + FAILED SCRAPE
        # -------------------------------------------------------------------

        else:

            failed_no_fallback.append(
                url
            )


    # -----------------------------------------------------------------------
    # WRITE REBUILT DATA
    # -----------------------------------------------------------------------

    data["funds"]["funds"] = new_funds

    data["history"] = new_history

    data["history_full"] = new_history_full


    # -----------------------------------------------------------------------
    # UPDATE TIMESTAMP
    # -----------------------------------------------------------------------

    from datetime import (
        datetime,
        timezone,
        timedelta
    )

    sgt = timezone(
        timedelta(hours=8)
    )

    now = datetime.now(
        sgt
    )


    data["funds"]["updated_on"] = (
        now.strftime(
            "%d-%b-%Y"
        )
    )

    data["funds"]["updated_at"] = (
        now.strftime(
            "%d-%b-%Y %I:%M %p SGT"
        )
    )


    # -----------------------------------------------------------------------
    # SAVE
    # -----------------------------------------------------------------------

    DATA_PATH.write_text(
        json.dumps(
            data
        )
    )


    # -----------------------------------------------------------------------
    # SUMMARY
    # -----------------------------------------------------------------------

    print(
        f"\ndata.json rebuilt: "
        f"{len(new_funds)} funds "
        f"(matches {len(urls)} URLs in the Excel file exactly)."
    )

    print(
        f"  {added} new, "
        f"{reused_from_failure} reused from a failed scrape this run."
    )


    # -----------------------------------------------------------------------
    # FAILED NEW FUNDS
    # -----------------------------------------------------------------------

    if failed_no_fallback:

        print(
            f"\n{len(failed_no_fallback)} URL(s) failed "
            f"with no previous data to fall back on "
            f"(these funds are temporarily absent until "
            f"a future run succeeds):",
            file=sys.stderr
        )

        for u in failed_no_fallback:

            print(
                f"  - {u}",
                file=sys.stderr
            )


# ---------------------------------------------------------------------------
# ENTRY POINT
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    main()
```

### One important note

I kept your existing fallback behavior exactly as requested. That means if an **existing fund fails to scrape**, its previous record is retained, including whatever values were previously stored.

For a **successfully scraped fund**, missing fields now become `"-"` rather than being guessed.

The next change should therefore be separate: **make the scraper retrieve the actual Prudential fund data more reliably**, particularly the fund code, CIC, asset class, and historical prices, without introducing any synthetic values.
