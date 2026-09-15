#!/usr/bin/env python3

"""
VGrat FMS - Prudential Fund Scraper

SOURCE OF TRUTH
---------------
Funds_Links.xlsm contains the fund URLs that should exist.

Each fund is identified by its source URL.

JSON STRUCTURE
--------------
This script preserves the existing data.json structure:

{
    "funds": {
        "funds": [...],
        "updated_on": "...",
        "updated_at": "..."
    },
    "history": {...},
    "indices": [...],
    "news": [...],
    "riskFactors": [...],
    "summary": {...},
    "commodities": [...],
    "currencies": [...],
    "bonds": [...],
    "history_full": {...}
}

IMPORTANT
---------
The scraper only replaces the nested:

    data["funds"]["funds"]

collection.

All other top-level data is preserved.

HOLDINGS
--------
Holdings are extracted from Prudential factsheet PDFs.

"Top 10 Holdings" means the name of the section. It does NOT mean
there must be exactly 10 holdings.

The scraper accepts 1-10 actual holdings.

No synthetic holdings are created.

HISTORY
-------
No synthetic historical price data is generated.

Existing history is preserved.

FAIL-SAFE
---------
An existing fund will never be deleted merely because Prudential
temporarily fails to return the page.

A completely empty scrape will NEVER overwrite the existing fund list.
"""

import argparse
import io
import json
import re
import sys
import time

from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.parse import urljoin


# ============================================================================
# PATHS
# ============================================================================

DATA_PATH = (
    Path(__file__).resolve().parent.parent / "data.json"
)


# ============================================================================
# FUND PAGE REGEX
# ============================================================================

FIELD_PATTERNS = {

    "risk": re.compile(
        r"Risk classification\s*\n+\s*\**"
        r"(Lower Risk|Low to Medium Risk|Medium to High Risk|Higher Risk)",
        re.I,
    ),

    "currency": re.compile(
        r"\bCurrency\s*\n+\s*\**([A-Z]{3})\b",
        re.I,
    ),

    "inception": re.compile(
        r"Inception date\s*\n+\s*\**(\d{1,2} \w{3} \d{4})",
        re.I,
    ),

    "code": re.compile(
        r"Fund code\s*\n+\s*\**([A-Z0-9]{3,8})\b",
        re.I,
    ),

    "cic": re.compile(
        r"Continuing Investment Charge[^\n]*\n[^\n]*\n\s*\**([\d.]+)%",
        re.I,
    ),

    "asset_class": re.compile(
        r"\n\**(Money Market|Fixed Income|Equity|Multi-Asset)\**"
        r"\s*\n\s*Risk classification",
        re.I,
    ),

    "bid": re.compile(
        r"Bid price\s*\n+\s*\$?([\d.]+)",
        re.I,
    ),

    "offer": re.compile(
        r"Offer price\s*\n+\s*\$?([\d.]+)",
        re.I,
    ),

    "return_1y": re.compile(
        r"1-year\s*\n+\s*([+-]?[\d.]+|-)\s*%",
        re.I,
    ),

    "return_3y": re.compile(
        r"3-year\s*\n+\s*([+-]?[\d.]+|-)\s*%?",
        re.I,
    ),

    "return_5y": re.compile(
        r"5-year\s*\n+\s*([+-]?[\d.]+|-)\s*%?",
        re.I,
    ),
}


NAME_PATTERN = re.compile(
    r"\b(PRU(?:Link|Prime)\s+[^\n]+)",
    re.I,
)


# ============================================================================
# HOLDINGS
# ============================================================================

TOP_HOLDINGS_PATTERN = re.compile(
    r"Top\s+(?:10\s+)?Holdings",
    re.I,
)

HOLDING_PERCENT_PATTERN = re.compile(
    r"^[+-]?\d+(?:\.\d+)?%$"
)

SOURCE_PATTERN = re.compile(
    r"^Source\s*:",
    re.I,
)


def clean_pdf_word(text):
    if text is None:
        return ""

    text = str(text)

    for char in (
        "\x00",
        "\x01",
        "\x02",
        "\x03",
        "\x04",
        "\x05",
        "\x06",
        "\ufeff",
    ):
        text = text.replace(char, "")

    return text.strip()


def is_percentage_text(text):
    if not text:
        return False

    return bool(
        HOLDING_PERCENT_PATTERN.match(
            clean_pdf_word(text)
        )
    )


def parse_percentage(text):
    try:
        text = clean_pdf_word(text)
        return float(text.replace("%", ""))
    except Exception:
        return None


def group_words_into_rows(words, y_tolerance=3.0):

    if not words:
        return []

    words = sorted(
        words,
        key=lambda w: (
            float(w["top"]),
            float(w["x0"]),
        ),
    )

    rows = []

    for word in words:

        top = float(word["top"])
        bottom = float(word["bottom"])

        center_y = (top + bottom) / 2

        found = None

        for row in rows:

            row_center = (
                row["top"] + row["bottom"]
            ) / 2

            if abs(center_y - row_center) <= y_tolerance:
                found = row
                break

        if found is None:

            rows.append(
                {
                    "top": top,
                    "bottom": bottom,
                    "words": [word],
                }
            )

        else:

            found["words"].append(word)

            found["top"] = min(
                found["top"],
                top,
            )

            found["bottom"] = max(
                found["bottom"],
                bottom,
            )

    for row in rows:

        row["words"].sort(
            key=lambda w: float(w["x0"])
        )

    rows.sort(
        key=lambda r: r["top"]
    )

    return rows


def row_text(row):

    return " ".join(
        clean_pdf_word(
            word.get("text", "")
        )
        for word in row["words"]
        if clean_pdf_word(
            word.get("text", "")
        )
    ).strip()


def clean_holding_name(name):

    if not name:
        return ""

    name = clean_pdf_word(name)

    name = re.sub(
        r"\s+",
        " ",
        name,
    ).strip()

    # ------------------------------------------------------------------
    # Remove obvious PDF table artefacts at the beginning.
    #
    # Examples:
    #
    # 120 Fidelity Funds SICAV - Global Dividend Fund
    #
    # should become:
    #
    # Fidelity Funds SICAV - Global Dividend Fund
    #
    # But don't remove meaningful alphanumeric fund names.
    # ------------------------------------------------------------------

    name = re.sub(
        r"^\d{1,4}\s+(?=[A-Za-z])",
        "",
        name,
    )

    # Remove isolated page/table numbering such as "1." / "01."
    name = re.sub(
        r"^\d{1,3}\.\s+(?=[A-Za-z])",
        "",
        name,
    )

    # Remove leading bullets.
    name = re.sub(
        r"^[•·▪●]\s*",
        "",
        name,
    )

    return name.strip()


def find_top_holdings_region(page, debug=False):

    words = page.extract_words(
        x_tolerance=1,
        y_tolerance=3,
        keep_blank_chars=False,
        use_text_flow=False,
    )

    if not words:
        return []

    cleaned = []

    for word in words:

        text = clean_pdf_word(
            word.get("text", "")
        )

        if not text:
            continue

        new_word = dict(word)
        new_word["text"] = text

        cleaned.append(new_word)

    rows = group_words_into_rows(
        cleaned,
        y_tolerance=3.5,
    )

    heading_index = None

    for index, row in enumerate(rows):

        text = row_text(row)

        if TOP_HOLDINGS_PATTERN.search(text):
            heading_index = index
            break

    if heading_index is None:

        if debug:
            print(
                "[debug] No Top Holdings heading found",
                file=sys.stderr,
            )

        return []

    heading_bottom = rows[
        heading_index
    ]["bottom"]

    end_top = None

    for row in rows[
        heading_index + 1:
    ]:

        text = row_text(row)

        lower = text.lower()

        if SOURCE_PATTERN.search(text):

            end_top = row["top"]
            break

        if (
            lower.startswith("sector allocation")
            or lower.startswith("country allocation")
            or lower.startswith("asset allocation")
            or lower.startswith("performance")
            or lower.startswith("calendar year performance")
            or lower.startswith("geographical allocation")
        ):

            end_top = row["top"]
            break

    if end_top is None:

        end_top = min(
            page.height,
            heading_bottom + 220,
        )

    return [
        word
        for word in cleaned
        if float(word["top"]) >= heading_bottom
        and float(word["top"]) < end_top
    ]


def extract_holdings_from_page(page, debug=False):

    region_words = find_top_holdings_region(
        page,
        debug=debug,
    )

    if not region_words:
        return []

    rows = group_words_into_rows(
        region_words,
        y_tolerance=3.5,
    )

    if debug:

        print(
            "[debug] Top Holdings rows:",
            file=sys.stderr,
        )

        for row in rows:

            print(
                f"[debug]   {row_text(row)}",
                file=sys.stderr,
            )

    percentage_rows = []

    for index, row in enumerate(rows):

        percentage_words = [
            word
            for word in row["words"]
            if is_percentage_text(
                word["text"]
            )
        ]

        if not percentage_words:
            continue

        percentage_word = max(
            percentage_words,
            key=lambda w: float(w["x0"]),
        )

        percentage = parse_percentage(
            percentage_word["text"]
        )

        if percentage is None:
            continue

        percentage_rows.append(
            {
                "row_index": index,
                "row": row,
                "percentage_word": percentage_word,
                "percentage": percentage,
            }
        )

    if not percentage_rows:
        return []

    holdings = []

    for item in percentage_rows[:10]:

        row_index = item["row_index"]

        current_row = item["row"]

        pct_word = item[
            "percentage_word"
        ]

        percentage = item[
            "percentage"
        ]

        pct_x0 = float(
            pct_word["x0"]
        )

        # --------------------------------------------------------------
        # Same-row words to the left of percentage.
        # --------------------------------------------------------------

        same_row_words = [
            word
            for word in current_row["words"]
            if float(word["x1"]) <= pct_x0 + 1
            and not is_percentage_text(
                word["text"]
            )
        ]

        same_row_text = " ".join(
            clean_pdf_word(
                word["text"]
            )
            for word in same_row_words
        ).strip()

        name_parts = []

        if same_row_text:

            name_parts.append(
                same_row_text
            )

        # --------------------------------------------------------------
        # Wrapped names.
        #
        # Only look backwards until another percentage row or heading.
        # --------------------------------------------------------------

        previous_rows = []

        j = row_index - 1

        while (
            j >= 0
            and len(previous_rows) < 2
        ):

            candidate = rows[j]

            candidate_text = row_text(
                candidate
            )

            if not candidate_text:
                j -= 1
                continue

            lower = candidate_text.lower()

            if (
                "top holdings" in lower
                or lower.startswith("source:")
                or lower.startswith("sector allocation")
                or lower.startswith("country allocation")
                or lower.startswith("asset allocation")
                or lower.startswith("performance")
            ):
                break

            if any(
                is_percentage_text(
                    word["text"]
                )
                for word in candidate["words"]
            ):
                break

            gap = (
                current_row["top"]
                - candidate["bottom"]
            )

            if gap > 12:
                break

            previous_rows.append(
                candidate
            )

            j -= 1

        previous_rows.reverse()

        previous_text = [
            row_text(row)
            for row in previous_rows
            if row_text(row)
        ]

        if previous_text:

            name_parts = (
                previous_text
                + name_parts
            )

        name = clean_holding_name(
            " ".join(name_parts)
        )

        if not name:
            continue

        # --------------------------------------------------------------
        # Reject obvious non-holding text.
        # --------------------------------------------------------------

        lower_name = name.lower()

        if lower_name in {
            "top holdings",
            "source",
            "performance",
            "performance chart",
        }:
            continue

        # Reject names that are only numbers.
        if re.fullmatch(
            r"[\d\s.,%-]+",
            name,
        ):
            continue

        holdings.append(
            {
                "name": name,
                "weight": percentage,
            }
        )

    # ------------------------------------------------------------------
    # Deduplicate.
    # ------------------------------------------------------------------

    result = []

    seen = set()

    for holding in holdings:

        key = (
            holding["name"].lower(),
            round(
                float(
                    holding["weight"]
                ),
                6,
            ),
        )

        if key in seen:
            continue

        seen.add(key)
        result.append(holding)

    return result[:10]


def extract_holdings_from_pdf(
    pdf_bytes,
    debug_url="",
    debug=False,
):

    try:
        import pdfplumber
    except ImportError:

        raise RuntimeError(
            "pdfplumber is required."
        )

    results = []

    with pdfplumber.open(
        io.BytesIO(pdf_bytes)
    ) as pdf:

        for page_number, page in enumerate(
            pdf.pages,
            start=1,
        ):

            page_holdings = (
                extract_holdings_from_page(
                    page,
                    debug=debug,
                )
            )

            if page_holdings:

                if debug:

                    print(
                        f"[debug] {debug_url}: "
                        f"page {page_number}: "
                        f"{len(page_holdings)} holdings",
                        file=sys.stderr,
                    )

                results.extend(
                    page_holdings
                )

    final = []

    seen = set()

    for holding in results:

        name = clean_holding_name(
            holding["name"]
        )

        weight = holding[
            "weight"
        ]

        if not name:
            continue

        key = (
            name.lower(),
            round(
                float(weight),
                6,
            ),
        )

        if key in seen:
            continue

        seen.add(key)

        final.append(
            {
                "name": name,
                "weight": weight,
            }
        )

    return final[:10]


# ============================================================================
# FACTSHEET
# ============================================================================

def fetch_holdings_from_factsheet(
    factsheet_url,
    debug=False,
):

    import requests

    response = requests.get(
        factsheet_url,
        timeout=30,
        headers={
            "User-Agent": (
                "Mozilla/5.0 "
                "(Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 "
                "(KHTML, like Gecko) "
                "Chrome/128.0 Safari/537.36"
            )
        },
    )

    response.raise_for_status()

    return extract_holdings_from_pdf(
        response.content,
        debug_url=factsheet_url,
        debug=debug,
    )


def find_factsheet_url(
    page,
    base_url,
):

    candidates = [
        "View factsheet",
        "Fund Factsheet",
    ]

    for text in candidates:

        try:

            locator = page.get_by_text(
                text,
                exact=False,
            )

            count = locator.count()

            for index in range(
                min(count, 5)
            ):

                element = locator.nth(
                    index
                )

                href = element.get_attribute(
                    "href"
                )

                if not href:
                    continue

                absolute = urljoin(
                    base_url,
                    href,
                )

                if (
                    ".pdf" in absolute.lower()
                    or "factsheet"
                    in absolute.lower()
                ):
                    return absolute

        except Exception:
            continue

    try:

        anchors = page.locator("a")

        count = anchors.count()

        for index in range(count):

            anchor = anchors.nth(
                index
            )

            text = (
                anchor.inner_text()
                .strip()
                .lower()
            )

            href = anchor.get_attribute(
                "href"
            )

            if not href:
                continue

            if (
                "factsheet" in text
                or "view factsheet" in text
            ):

                absolute = urljoin(
                    base_url,
                    href,
                )

                return absolute

    except Exception:
        pass

    return ""


# ============================================================================
# FUND PAGE
# ============================================================================

def parse_fund_page(
    text,
    url,
    debug=False,
):

    result = {
        "url": url,
        "scraped_name": None,
        "risk": None,
        "currency": None,
        "inception": None,
        "code": None,
        "cic": None,
        "asset_class": None,
        "bid": None,
        "offer": None,
        "return_1y": None,
        "return_3y": None,
        "return_5y": None,
    }

    match = NAME_PATTERN.search(
        text
    )

    if match:

        result[
            "scraped_name"
        ] = match.group(1).strip(
            " *"
        )

    for key, pattern in FIELD_PATTERNS.items():

        match = pattern.search(
            text
        )

        if match:

            result[key] = (
                match.group(1).strip()
            )

    if debug:

        print(
            f"[debug] {url}",
            file=sys.stderr,
        )

        print(
            f"[debug] name="
            f"{result['scraped_name']!r}",
            file=sys.stderr,
        )

        print(
            f"[debug] code="
            f"{result['code']!r}",
            file=sys.stderr,
        )

        print(
            f"[debug] bid="
            f"{result['bid']!r}",
            file=sys.stderr,
        )

    return result


def fetch_and_parse(
    url,
    debug=False,
):

    from playwright.sync_api import (
        sync_playwright,
    )

    with sync_playwright() as p:

        browser = p.chromium.launch(
            headless=True
        )

        page = browser.new_page()

        try:

            page.goto(
                url,
                wait_until="networkidle",
                timeout=30000,
            )

            text = page.inner_text(
                "body"
            )

            factsheet_url = (
                find_factsheet_url(
                    page,
                    url,
                )
            )

        finally:

            browser.close()

    result = parse_fund_page(
        text,
        url,
        debug=debug,
    )

    result[
        "holdings"
    ] = []

    if factsheet_url:

        try:

            result[
                "holdings"
            ] = fetch_holdings_from_factsheet(
                factsheet_url,
                debug=debug,
            )

        except Exception as exc:

            if debug:

                print(
                    f"[debug] Factsheet failed: "
                    f"{exc}",
                    file=sys.stderr,
                )

    result[
        "factsheetUrl"
    ] = factsheet_url or "-"

    return result


# ============================================================================
# EXCEL
# ============================================================================

def load_urls_from_excel(
    path,
):

    import openpyxl

    wb = openpyxl.load_workbook(
        path,
        data_only=True,
        keep_vba=True,
    )

    ws = wb[
        wb.sheetnames[0]
    ]

    urls = []

    seen = set()

    for row in ws.iter_rows(
        min_row=2,
        values_only=True,
    ):

        if not row:
            continue

        value = row[0]

        if not value:
            continue

        url = str(
            value
        ).strip()

        if not url:
            continue

        if url in seen:
            continue

        seen.add(url)
        urls.append(url)

    return urls


# ============================================================================
# CATEGORY
# ============================================================================

ASSET_CLASS_TO_CATEGORY = {
    "money market": "Cash",
    "fixed income": "Fixed Income",
    "multi-asset": "Multi-Asset",
    "equity": "Global Equity",
}


def guess_category(
    scraped,
):

    name = (
        scraped.get(
            "scraped_name"
        )
        or ""
    ).lower()

    hints = [
        ("greater china", "China Equity"),
        ("china", "China Equity"),
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

    for hint, category in hints:

        if hint in name:
            return category

    asset_class = (
        scraped.get(
            "asset_class"
        )
        or ""
    ).lower()

    return ASSET_CLASS_TO_CATEGORY.get(
        asset_class,
        "-",
    )


# ============================================================================
# CONVERSIONS
# ============================================================================

def safe_float(value):

    if value is None:
        return None

    if isinstance(
        value,
        (int, float),
    ):
        return float(value)

    try:

        value = str(
            value
        ).strip()

        if not value or value == "-":
            return None

        return float(value)

    except Exception:
        return None


def value_or_dash(value):

    if value is None:
        return "-"

    value = str(
        value
    ).strip()

    return value or "-"


# ============================================================================
# EXISTING FUND EXTRACTION
# ============================================================================

def get_existing_funds(
    data,
):

    container = data.get(
        "funds",
        {},
    )

    # --------------------------------------------------------------
    # Current expected structure:
    #
    # data["funds"]["funds"]
    # --------------------------------------------------------------

    if isinstance(
        container,
        dict,
    ):

        funds = container.get(
            "funds",
            [],
        )

        if isinstance(
            funds,
            list,
        ):
            return funds

    # --------------------------------------------------------------
    # Compatibility with older possible structure:
    #
    # data["funds"] = [...]
    # --------------------------------------------------------------

    if isinstance(
        container,
        list,
    ):
        return container

    return []


# ============================================================================
# MAIN
# ============================================================================

def main():

    parser = argparse.ArgumentParser()

    parser.add_argument(
        "--urls",
        required=False,
    )

    parser.add_argument(
        "--input-html",
    )

    parser.add_argument(
        "--delay",
        type=float,
        default=1.5,
    )

    parser.add_argument(
        "--debug",
        action="store_true",
    )

    parser.add_argument(
        "--dry-run",
        action="store_true",
    )

    args = parser.parse_args()

    # ------------------------------------------------------------------
    # Input text test
    # ------------------------------------------------------------------

    if args.input_html:

        text = Path(
            args.input_html
        ).read_text(
            encoding="utf-8"
        )

        parsed = parse_fund_page(
            text,
            args.input_html,
            debug=True,
        )

        print(
            json.dumps(
                parsed,
                indent=2,
                ensure_ascii=False,
            )
        )

        return

    if not args.urls:

        print(
            "ERROR: --urls is required.",
            file=sys.stderr,
        )

        sys.exit(1)

    # ------------------------------------------------------------------
    # Excel URLs
    # ------------------------------------------------------------------

    urls = load_urls_from_excel(
        args.urls
    )

    print(
        f"Loaded {len(urls)} unique fund URLs."
    )

    if not urls:

        print(
            "ERROR: No fund URLs found.",
            file=sys.stderr,
        )

        sys.exit(1)

    # ------------------------------------------------------------------
    # Load existing data FIRST.
    #
    # This allows us to protect existing data if scraping fails.
    # ------------------------------------------------------------------

    if not DATA_PATH.exists():

        print(
            f"ERROR: {DATA_PATH} does not exist.",
            file=sys.stderr,
        )

        sys.exit(1)

    try:

        data = json.loads(
            DATA_PATH.read_text(
                encoding="utf-8"
            )
        )

    except Exception as exc:

        print(
            f"ERROR: Cannot parse data.json: {exc}",
            file=sys.stderr,
        )

        sys.exit(1)

    old_funds = get_existing_funds(
        data
    )

    print(
        f"Existing fund records: {len(old_funds)}"
    )

    old_by_url = {}

    for fund in old_funds:

        if not isinstance(
            fund,
            dict,
        ):
            continue

        source_url = fund.get(
            "sourceUrl"
        )

        if source_url:

            old_by_url[
                source_url
            ] = fund

    # ------------------------------------------------------------------
    # Scrape
    # ------------------------------------------------------------------

    scraped_by_url = {}

    failed_urls = []

    for index, url in enumerate(
        urls,
        start=1,
    ):

        try:

            result = fetch_and_parse(
                url,
                debug=args.debug,
            )

            scraped_by_url[
                url
            ] = result

            print(
                f"[{index}/{len(urls)}] "
                f"{result.get('scraped_name') or url} "
                f"code={result.get('code') or '-'} "
                f"bid={result.get('bid') or '-'} "
                f"holdings="
                f"{len(result.get('holdings') or [])}"
            )

        except Exception as exc:

            failed_urls.append(
                url
            )

            print(
                f"[{index}/{len(urls)}] "
                f"FAILED: {url}",
                file=sys.stderr,
            )

            print(
                f"    {exc}",
                file=sys.stderr,
            )

        time.sleep(
            args.delay
        )

    # ------------------------------------------------------------------
    # Dry run
    # ------------------------------------------------------------------

    if args.dry_run:

        Path(
            "scraped_raw.json"
        ).write_text(
            json.dumps(
                list(
                    scraped_by_url.values()
                ),
                indent=2,
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        print(
            "Dry run complete."
        )

        return

    # ------------------------------------------------------------------
    # CRITICAL FAIL-SAFE
    #
    # If nothing was successfully scraped, DO NOT TOUCH the existing
    # fund list.
    # ------------------------------------------------------------------

    if not scraped_by_url:

        print(
            "\nERROR: ZERO fund pages were successfully scraped.",
            file=sys.stderr,
        )

        print(
            "Existing fund data will NOT be replaced.",
            file=sys.stderr,
        )

        sys.exit(1)

    # ------------------------------------------------------------------
    # Rebuild fund list
    # ------------------------------------------------------------------

    new_funds = []

    new_history = {}

    new_history_full = {}

    old_history = data.get(
        "history",
        {},
    )

    old_history_full = data.get(
        "history_full",
        {},
    )

    successful = 0
    reused = 0
    added = 0

    # ------------------------------------------------------------------
    # Excel order is authoritative.
    # ------------------------------------------------------------------

    for url in urls:

        live = scraped_by_url.get(
            url
        )

        previous = old_by_url.get(
            url
        )

        # ==============================================================
        # LIVE SCRAPE AVAILABLE
        # ==============================================================

        if live and live.get(
            "scraped_name"
        ):

            successful += 1

            name = str(
                live["scraped_name"]
            ).strip()

            bid = safe_float(
                live.get("bid")
            )

            offer = safe_float(
                live.get("offer")
            )

            # ----------------------------------------------------------
            # Important:
            #
            # Missing bid does NOT invalidate the whole fund anymore.
            # ----------------------------------------------------------

            if bid is None and previous:

                bid = previous.get(
                    "bid",
                    "-",
                )

            if bid is None:

                bid = "-"

            # ----------------------------------------------------------
            # Offer
            # ----------------------------------------------------------

            if offer is None and previous:

                offer = previous.get(
                    "offer",
                    "-",
                )

            if offer is None:

                offer = "-"

            # ----------------------------------------------------------
            # Scalar fields
            # ----------------------------------------------------------

            code = value_or_dash(
                live.get("code")
            )

            if code == "-" and previous:

                code = previous.get(
                    "code",
                    "-",
                )

            risk = value_or_dash(
                live.get("risk")
            )

            if risk == "-" and previous:

                risk = previous.get(
                    "riskCategory",
                    "-",
                )

            currency = value_or_dash(
                live.get("currency")
            )

            if currency == "-" and previous:

                currency = previous.get(
                    "currency",
                    "-",
                )

            inception = value_or_dash(
                live.get("inception")
            )

            if inception == "-" and previous:

                inception = previous.get(
                    "effective_date",
                    "-",
                )

            cic = value_or_dash(
                live.get("cic")
            )

            if cic == "-" and previous:

                cic = previous.get(
                    "cic",
                    "-",
                )

            asset_class = value_or_dash(
                live.get("asset_class")
            )

            if asset_class == "-" and previous:

                asset_class = previous.get(
                    "assetClass",
                    "-",
                )

            # ----------------------------------------------------------
            # Category
            # ----------------------------------------------------------

            category = guess_category(
                live
            )

            if (
                category == "-"
                and previous
            ):

                category = previous.get(
                    "category",
                    "-",
                )

            # ----------------------------------------------------------
            # Holdings
            #
            # Only replace existing holdings if the new PDF actually
            # returned holdings.
            # ----------------------------------------------------------

            new_holdings = (
                live.get(
                    "holdings"
                )
                or []
            )

            if not new_holdings and previous:

                new_holdings = previous.get(
                    "holdings",
                    [],
                )

            # ----------------------------------------------------------
            # Factsheet
            # ----------------------------------------------------------

            factsheet = value_or_dash(
                live.get(
                    "factsheetUrl"
                )
            )

            if (
                factsheet == "-"
                and previous
            ):

                factsheet = previous.get(
                    "factsheetUrl",
                    "-",
                )

            # ----------------------------------------------------------
            # Fund object
            # ----------------------------------------------------------

            fund = {

                "name": name,

                "category": category,

                "currency": currency,

                "effective_date": inception,

                "bid": bid,

                "offer": offer,

                "code": code,

                "codeVerified": (
                    code != "-"
                ),

                "riskCategory": risk,

                "cic": cic,

                "assetClass": asset_class,

                "dataSource": "verified-live",

                "sourceUrl": url,

                "factsheetUrl": factsheet,

                "holdings": new_holdings,

            }

            # ----------------------------------------------------------
            # Live annualised returns
            # ----------------------------------------------------------

            live_returns = {}

            for period, key in (
                ("1y", "return_1y"),
                ("3y", "return_3y"),
                ("5y", "return_5y"),
            ):

                value = safe_float(
                    live.get(key)
                )

                if value is None and previous:

                    value = safe_float(
                        previous.get(
                            "liveReturns",
                            {},
                        ).get(
                            period
                        )
                    )

                live_returns[
                    period
                ] = (
                    value
                    if value is not None
                    else "-"
                )

            fund[
                "liveReturns"
            ] = live_returns

            new_funds.append(
                fund
            )

            # ----------------------------------------------------------
            # History
            # ----------------------------------------------------------

            old_name = (
                previous.get(
                    "name"
                )
                if previous
                else None
            )

            if old_name:

                if old_name in old_history:

                    new_history[
                        name
                    ] = old_history[
                        old_name
                    ]

                if old_name in old_history_full:

                    new_history_full[
                        name
                    ] = old_history_full[
                        old_name
                    ]

            if not previous:

                added += 1

            continue

        # ==============================================================
        # LIVE SCRAPE FAILED
        # ==============================================================

        if previous:

            # ----------------------------------------------------------
            # Preserve the previous record.
            # ----------------------------------------------------------

            new_funds.append(
                previous
            )

            reused += 1

            old_name = previous.get(
                "name"
            )

            if old_name:

                if old_name in old_history:

                    new_history[
                        old_name
                    ] = old_history[
                        old_name
                    ]

                if old_name in old_history_full:

                    new_history_full[
                        old_name
                    ] = old_history_full[
                        old_name
                    ]

            print(
                f"  Reused previous record: {url}",
                file=sys.stderr,
            )

            continue

        # --------------------------------------------------------------
        # Brand-new URL that could not be scraped.
        #
        # Do not fabricate a fund record.
        # --------------------------------------------------------------

        print(
            f"  WARNING: New URL could not be scraped: {url}",
            file=sys.stderr,
        )

    # ------------------------------------------------------------------
    # SAFETY CHECK
    #
    # Never replace a populated existing fund list with zero.
    # ------------------------------------------------------------------

    if (
        len(new_funds) == 0
        and len(old_funds) > 0
    ):

        print(
            "\nERROR: Rebuild produced ZERO funds "
            "while existing data contains funds.",
            file=sys.stderr,
        )

        print(
            "data.json was NOT modified.",
            file=sys.stderr,
        )

        sys.exit(1)

    # ------------------------------------------------------------------
    # Make sure the nested funds structure exists.
    # ------------------------------------------------------------------

    if not isinstance(
        data.get("funds"),
        dict,
    ):

        data[
            "funds"
        ] = {}

    # ------------------------------------------------------------------
    # Replace ONLY the fund array.
    # ------------------------------------------------------------------

    data[
        "funds"
    ][
        "funds"
    ] = new_funds

    # ------------------------------------------------------------------
    # Preserve history only for funds still present.
    # ------------------------------------------------------------------

    data[
        "history"
    ] = new_history

    data[
        "history_full"
    ] = new_history_full

    # ------------------------------------------------------------------
    # Timestamp
    # ------------------------------------------------------------------

    sgt = timezone(
        timedelta(
            hours=8
        )
    )

    now = datetime.now(
        sgt
    )

    data[
        "funds"
    ][
        "updated_on"
    ] = now.strftime(
        "%d-%b-%Y"
    )

    data[
        "funds"
    ][
        "updated_at"
    ] = now.strftime(
        "%d-%b-%Y %I:%M %p SGT"
    )

    # ------------------------------------------------------------------
    # Write JSON
    # ------------------------------------------------------------------

    DATA_PATH.write_text(
        json.dumps(
            data,
            ensure_ascii=False,
            separators=(
                ",",
                ":",
            ),
        ),
        encoding="utf-8",
    )

    # ------------------------------------------------------------------
    # Final report
    # ------------------------------------------------------------------

    print()
    print(
        "=================================================="
    )
    print(
        "VGrat FMS FUND REBUILD COMPLETE"
    )
    print(
        "=================================================="
    )

    print(
        f"Excel URLs:              {len(urls)}"
    )

    print(
        f"Live scrapes:            {successful}"
    )

    print(
        f"Existing records reused: {reused}"
    )

    print(
        f"New funds:               {added}"
    )

    print(
        f"Funds written:           {len(new_funds)}"
    )

    print(
        f"Failed URLs:             {len(failed_urls)}"
    )

    print(
        "=================================================="
    )

    if len(new_funds) == len(urls):

        print(
            "SUCCESS: Fund count matches Funds_Links.xlsm."
        )

    elif len(new_funds) > 0:

        print(
            "WARNING: Some new URLs could not be scraped."
        )

        print(
            f"Written {len(new_funds)} of {len(urls)} URLs."
        )

    print()


if __name__ == "__main__":
    main()
