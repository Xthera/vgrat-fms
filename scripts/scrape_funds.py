#!/usr/bin/env python3
"""
Rebuilds data.json's entire fund list from scratch every run, using
Funds_Links.xlsm as the single source of truth for which funds should
exist.

Each fund is keyed by its source URL, not by its name.

Therefore:

  - Add a row to Funds_Links.xlsm -> fund appears on next run.
  - Remove a row -> fund disappears on next run.
  - No stale fund records accumulate.
  - Duplicate URLs are removed automatically.
  - Existing data is retained only as a fallback when a live scrape fails.

LIVE FUND DATA
--------------
Fund pages are scraped from Prudential's live fund pages.

The scraper attempts to retrieve:

  - Fund name
  - Risk classification
  - Currency
  - Inception date
  - Fund code
  - Continuing Investment Charge
  - Asset class
  - Bid price
  - Offer price
  - 1-year return
  - 3-year return
  - 5-year return

If a scalar value cannot be retrieved, "-" is stored instead of
inventing a value.

HOLDINGS
--------
Top Holdings are extracted from the fund's Prudential factsheet PDF.

IMPORTANT:

"Top 10 Holdings" is the name of the Prudential section. It does NOT
mean every fund must contain exactly 10 holdings.

The scraper therefore accepts:

  1 holding
  2 holdings
  ...
  10 holdings

and returns only the holdings actually published by Prudential.

The parser uses PDF word coordinates rather than relying entirely on
pdfplumber.extract_text(), because Prudential factsheets can contain
wrapped holding names and multi-column layouts.

If a holding cannot be paired with its percentage confidently, it is
NOT guessed or fabricated.

HISTORY
-------
Synthetic price history is NOT generated.

Existing history is preserved when a fund already has historical data
in data.json.

New funds do not receive fabricated historical prices.

HISTORICAL RETURNS
------------------
This script currently does not create new historical price data from
the Prudential website. Therefore the dashboard should only use
historical data that actually exists in data.json.

USAGE
-----
    pip install playwright openpyxl pdfplumber requests

    playwright install chromium

    python scripts/scrape_funds.py --urls Funds_Links.xlsm

Test parser:

    python scripts/scrape_funds.py --input-html saved_page.txt --debug

Dry run:

    python scripts/scrape_funds.py --urls Funds_Links.xlsm --dry-run --debug
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


DATA_PATH = Path(__file__).resolve().parent.parent / "data.json"


# ---------------------------------------------------------------------------
# FUND PAGE REGEX
# ---------------------------------------------------------------------------

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
        r"Fund code\s*\n+\s*\**([A-Z0-9]{3,6})\b",
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


# ---------------------------------------------------------------------------
# HOLDINGS PARSER
# ---------------------------------------------------------------------------

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


def clean_pdf_word(text: str) -> str:
    """
    Cleans text returned by pdfplumber without aggressively changing
    company/fund names.

    Prudential PDFs can contain unusual control characters such as
    \\x02 in extracted text. These are removed.
    """
    if text is None:
        return ""

    text = text.replace("\x00", "")
    text = text.replace("\x01", "")
    text = text.replace("\x02", "")
    text = text.replace("\x03", "")
    text = text.replace("\ufeff", "")

    return text.strip()


def is_percentage_text(text: str) -> bool:
    """
    Determines whether a PDF word is a standalone percentage.
    """
    if not text:
        return False

    cleaned = clean_pdf_word(text)
    return bool(HOLDING_PERCENT_PATTERN.match(cleaned))


def parse_percentage(text: str):
    """
    Converts '82.1%' to 82.1.
    """
    try:
        cleaned = clean_pdf_word(text)
        cleaned = cleaned.replace("%", "")
        return float(cleaned)
    except Exception:
        return None


def word_center(word: dict) -> tuple:
    """
    Returns x/y center of a pdfplumber word.
    """
    x = (float(word["x0"]) + float(word["x1"])) / 2
    y = (float(word["top"]) + float(word["bottom"])) / 2
    return x, y


def group_words_into_rows(words: list, y_tolerance: float = 3.0) -> list:
    """
    Groups PDF words into visual rows using their vertical position.

    Returns:

        [
            {
                "top": ...,
                "bottom": ...,
                "words": [...]
            },
            ...
        ]

    Words are sorted left-to-right within each row.
    """

    if not words:
        return []

    sorted_words = sorted(
        words,
        key=lambda w: (
            float(w["top"]),
            float(w["x0"]),
        ),
    )

    rows = []

    for word in sorted_words:
        top = float(word["top"])
        bottom = float(word["bottom"])

        placed = False

        for row in rows:
            row_center = (
                row["top"] + row["bottom"]
            ) / 2

            word_center_y = (
                top + bottom
            ) / 2

            if abs(word_center_y - row_center) <= y_tolerance:
                row["words"].append(word)

                row["top"] = min(
                    row["top"],
                    top,
                )

                row["bottom"] = max(
                    row["bottom"],
                    bottom,
                )

                placed = True
                break

        if not placed:
            rows.append(
                {
                    "top": top,
                    "bottom": bottom,
                    "words": [word],
                }
            )

    for row in rows:
        row["words"].sort(
            key=lambda w: float(w["x0"])
        )

    rows.sort(
        key=lambda r: r["top"]
    )

    return rows


def row_text(row: dict) -> str:
    """
    Converts a visual row back into readable text.
    """
    return " ".join(
        clean_pdf_word(w["text"])
        for w in row["words"]
        if clean_pdf_word(w["text"])
    ).strip()


def find_top_holdings_region(page, debug=False):
    """
    Locates the visual region between:

        Top 10 Holdings

    and the next Source:/section boundary.

    Returns PDF words belonging to the holdings region.

    This is intentionally coordinate-based rather than based solely on
    extract_text(), because wrapped names can otherwise become detached
    from their percentages.
    """

    words = page.extract_words(
        x_tolerance=1,
        y_tolerance=3,
        keep_blank_chars=False,
        use_text_flow=False,
    )

    if not words:
        return []

    # Clean words first.
    for word in words:
        word["text"] = clean_pdf_word(word.get("text", ""))

    words = [
        w for w in words
        if w.get("text")
    ]

    rows = group_words_into_rows(words)

    heading_index = None

    # Find "Top 10 Holdings" even if pdfplumber separates it into words.
    for i, row in enumerate(rows):
        text = row_text(row)

        if TOP_HOLDINGS_PATTERN.search(text):
            heading_index = i
            break

    if heading_index is None:
        if debug:
            print(
                "[debug] No Top Holdings heading found in PDF",
                file=sys.stderr,
            )
        return []

    heading_bottom = rows[heading_index]["bottom"]

    # Locate Source: or another obvious boundary after the holdings heading.
    end_top = None

    for row in rows[heading_index + 1:]:
        text = row_text(row)

        if SOURCE_PATTERN.search(text):
            end_top = row["top"]
            break

        # Stop at common section boundaries if encountered.
        lower = text.lower()

        if (
            lower.startswith("sector allocation")
            or lower.startswith("country allocation")
            or lower.startswith("asset allocation")
            or lower.startswith("performance")
            or lower.startswith("calendar year performance")
        ):
            end_top = row["top"]
            break

    if end_top is None:
        # Conservative fallback: use a reasonable portion of the page.
        end_top = min(
            page.height,
            heading_bottom + 180,
        )

    region_words = [
        w
        for w in words
        if float(w["top"]) >= heading_bottom
        and float(w["top"]) < end_top
    ]

    return region_words


def extract_holdings_from_page(page, debug=False) -> list:
    """
    Extracts holdings from a single factsheet page.

    Strategy:

    1. Find Top Holdings section.
    2. Convert PDF words into visual rows.
    3. Find rows containing percentages.
    4. Build the holding name from text immediately preceding / beside
       that percentage.
    5. If a holding name wraps onto the previous visual row, merge it.
    6. Do not guess when the association is ambiguous.
    7. Return at most 10 holdings.

    This function intentionally allows fewer than 10 holdings.
    """

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

    # ------------------------------------------------------------------
    # Identify percentage rows.
    # ------------------------------------------------------------------

    percentage_rows = []

    for index, row in enumerate(rows):
        percentage_words = [
            w
            for w in row["words"]
            if is_percentage_text(w["text"])
        ]

        if not percentage_words:
            continue

        # Usually the percentage is the rightmost percentage in the row.
        percentage_word = max(
            percentage_words,
            key=lambda w: float(w["x0"]),
        )

        pct = parse_percentage(
            percentage_word["text"]
        )

        if pct is None:
            continue

        percentage_rows.append(
            {
                "row_index": index,
                "row": row,
                "percentage_word": percentage_word,
                "percentage": pct,
            }
        )

    if not percentage_rows:
        if debug:
            print(
                "[debug] Top Holdings section found but no percentage rows detected",
                file=sys.stderr,
            )
        return []

    holdings = []

    # ------------------------------------------------------------------
    # Build holding names.
    # ------------------------------------------------------------------

    for item_no, pct_info in enumerate(
        percentage_rows[:10],
        start=1,
    ):
        row_index = pct_info["row_index"]
        pct_word = pct_info["percentage_word"]
        pct = pct_info["percentage"]

        pct_x0 = float(pct_word["x0"])

        current_row = rows[row_index]

        # Words immediately to the LEFT of the percentage on the same row.
        same_row_words = [
            w
            for w in current_row["words"]
            if float(w["x1"]) <= pct_x0 + 1
            and not is_percentage_text(w["text"])
        ]

        same_row_text = " ".join(
            clean_pdf_word(w["text"])
            for w in same_row_words
        ).strip()

        # --------------------------------------------------------------
        # If the current row contains a name, use it.
        # --------------------------------------------------------------

        name_parts = []

        if same_row_text:
            name_parts.append(same_row_text)

        # --------------------------------------------------------------
        # Wrapped holding names.
        #
        # If the current row has only the percentage, look backwards
        # for the immediately preceding text row.
        #
        # If the current row has a partial name, we also allow one
        # preceding text-only row to be joined.
        # --------------------------------------------------------------

        previous_rows = []

        j = row_index - 1

        while j >= 0 and len(previous_rows) < 3:
            candidate = rows[j]

            candidate_text = row_text(candidate)

            if not candidate_text:
                j -= 1
                continue

            # Stop at obvious section headings.
            lower = candidate_text.lower()

            if (
                "top holdings" in lower
                or lower.startswith("source:")
                or lower.startswith("sector allocation")
                or lower.startswith("country allocation")
                or lower.startswith("asset allocation")
            ):
                break

            candidate_has_percentage = any(
                is_percentage_text(w["text"])
                for w in candidate["words"]
            )

            if candidate_has_percentage:
                break

            previous_rows.append(candidate)

            # Once we found a row that appears to contain a proper
            # holding name, one additional preceding row is unnecessary
            # unless the name is obviously short.
            if len(candidate_text) > 12:
                break

            j -= 1

        # Reverse so the earlier wrapped line comes first.
        previous_rows.reverse()

        previous_text_parts = [
            row_text(r)
            for r in previous_rows
            if row_text(r)
        ]

        # --------------------------------------------------------------
        # Avoid accidentally attaching unrelated text.
        #
        # If same-row text exists, a preceding row is only merged when
        # the previous row is spatially close to the current row.
        # --------------------------------------------------------------

        if same_row_text:
            if previous_text_parts:
                vertical_gap = (
                    current_row["top"]
                    - previous_rows[-1]["bottom"]
                )

                if vertical_gap <= 10:
                    name_parts = (
                        previous_text_parts
                        + name_parts
                    )
        else:
            if previous_text_parts:
                name_parts = previous_text_parts

        name = " ".join(
            part.strip()
            for part in name_parts
            if part.strip()
        ).strip()

        # --------------------------------------------------------------
        # Remove obvious non-holding text.
        # --------------------------------------------------------------

        name = re.sub(
            r"\s+",
            " ",
            name,
        ).strip()

        if not name:
            if debug:
                print(
                    f"[debug] Holding #{item_no}: percentage {pct}% "
                    f"found but no reliable name",
                    file=sys.stderr,
                )
            continue

        # Reject obvious unrelated values.
        if name.lower() in {
            "top 10 holdings",
            "source",
            "performance",
            "performance chart",
        }:
            continue

        holdings.append(
            {
                "name": name,
                "weight": pct,
            }
        )

    # ------------------------------------------------------------------
    # Remove accidental duplicates while preserving order.
    # ------------------------------------------------------------------

    deduped = []
    seen = set()

    for holding in holdings:
        key = (
            holding["name"].strip().lower(),
            round(float(holding["weight"]), 6),
        )

        if key in seen:
            continue

        seen.add(key)
        deduped.append(holding)

    return deduped[:10]


def parse_holdings(text: str, debug_url: str = "") -> list:
    """
    Compatibility parser for --input-html / plain extracted text.

    This is NOT the primary factsheet parser.

    It attempts to recover holdings from plain text when a saved text
    file is supplied.

    For actual PDF scraping, extract_holdings_from_pdf() is used because
    coordinates are required for reliable wrapped-name handling.
    """

    if not text:
        return []

    # Locate the Top Holdings section.
    match = re.search(
        r"Top\s+(?:10\s+)?Holdings\b(.*?)(?:\n\s*Source\s*:|\Z)",
        text,
        re.I | re.S,
    )

    if not match:
        return []

    block = match.group(1)

    # Normalize PDF control characters.
    block = (
        block
        .replace("\x00", "")
        .replace("\x01", "")
        .replace("\x02", "")
        .replace("\x03", "")
    )

    lines = [
        re.sub(r"\s+", " ", line).strip()
        for line in block.splitlines()
    ]

    lines = [
        line
        for line in lines
        if line
    ]

    holdings = []

    # A percentage can be attached to the end of a line.
    attached_pattern = re.compile(
        r"^(.*?)\s+([+-]?\d+(?:\.\d+)?)%\s*$"
    )

    pending_name_parts = []

    for line in lines:
        m = attached_pattern.match(line)

        if m:
            name = m.group(1).strip()
            pct = float(m.group(2))

            if pending_name_parts:
                name = " ".join(
                    pending_name_parts + [name]
                ).strip()

            pending_name_parts = []

            if name:
                holdings.append(
                    {
                        "name": name,
                        "weight": pct,
                    }
                )

            continue

        # Standalone percentage.
        if re.match(
            r"^[+-]?\d+(?:\.\d+)?%\s*$",
            line,
        ):
            if pending_name_parts:
                pct = float(
                    line.replace("%", "").strip()
                )

                holdings.append(
                    {
                        "name": " ".join(
                            pending_name_parts
                        ),
                        "weight": pct,
                    }
                )

                pending_name_parts = []

            continue

        pending_name_parts.append(line)

    if debug_url and not holdings:
        print(
            f"[debug] {debug_url}: no holdings recovered from plain text",
            file=sys.stderr,
        )

    return holdings[:10]


def extract_holdings_from_pdf(
    pdf_bytes: bytes,
    debug_url: str = "",
    debug: bool = False,
) -> list:
    """
    Opens a Prudential factsheet PDF and extracts holdings using
    coordinate-aware parsing.

    Searches all pages because the layout can vary.
    """

    try:
        import pdfplumber
    except ImportError:
        raise RuntimeError(
            "pdfplumber is required for holdings extraction. "
            "Install it with: pip install pdfplumber"
        )

    all_holdings = []

    with pdfplumber.open(
        io.BytesIO(pdf_bytes)
    ) as pdf:

        for page_number, page in enumerate(
            pdf.pages,
            start=1,
        ):
            page_holdings = extract_holdings_from_page(
                page,
                debug=debug,
            )

            if page_holdings:
                if debug:
                    print(
                        f"[debug] {debug_url}: page {page_number} "
                        f"found {len(page_holdings)} holdings",
                        file=sys.stderr,
                    )

                all_holdings.extend(
                    page_holdings
                )

    # Deduplicate.
    result = []
    seen = set()

    for holding in all_holdings:
        key = (
            holding["name"].strip().lower(),
            round(float(holding["weight"]), 6),
        )

        if key in seen:
            continue

        seen.add(key)
        result.append(holding)

    return result[:10]


def fetch_holdings_from_factsheet(
    factsheet_url: str,
    debug: bool = False,
) -> list:
    """
    Downloads a Prudential factsheet and extracts real Top Holdings.
    """

    import requests

    response = requests.get(
        factsheet_url,
        timeout=30,
        headers={
            "User-Agent": (
                "Mozilla/5.0 "
                "(Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 "
                "Chrome/128.0 Safari/537.36"
            )
        },
    )

    response.raise_for_status()

    return extract_holdings_from_pdf(
        response.content,
        debug_url=factsheet_url if debug else "",
        debug=debug,
    )


# ---------------------------------------------------------------------------
# FACTSHEET LINK
# ---------------------------------------------------------------------------

def find_factsheet_url(page, base_url: str = "") -> str:
    """
    Finds the Prudential factsheet PDF link.

    Tries both:
        View factsheet
        Fund Factsheet

    Relative URLs are converted to absolute URLs.
    """

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

            if count == 0:
                continue

            for index in range(
                min(count, 5)
            ):
                element = locator.nth(index)

                href = element.get_attribute(
                    "href"
                )

                if not href:
                    continue

                absolute_url = urljoin(
                    base_url,
                    href,
                )

                if (
                    ".pdf" in absolute_url.lower()
                    or "factsheet" in absolute_url.lower()
                ):
                    return absolute_url

        except Exception:
            continue

    # Fallback: inspect all anchors.
    try:
        anchors = page.locator("a")

        count = anchors.count()

        for index in range(count):
            anchor = anchors.nth(index)

            text_content = (
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
                "factsheet" in text_content
                or "view factsheet" in text_content
            ):
                absolute_url = urljoin(
                    base_url,
                    href,
                )

                if (
                    ".pdf" in absolute_url.lower()
                    or "factsheet" in absolute_url.lower()
                ):
                    return absolute_url

    except Exception:
        pass

    return ""


# ---------------------------------------------------------------------------
# FUND PAGE PARSER
# ---------------------------------------------------------------------------

def parse_fund_page(
    text: str,
    url: str,
    debug: bool = False,
) -> dict:

    result = {
        "url": url
    }

    name_match = NAME_PATTERN.search(
        text
    )

    result["scraped_name"] = (
        name_match.group(1).strip(" *")
        if name_match
        else None
    )

    for key, pattern in FIELD_PATTERNS.items():
        match = pattern.search(text)

        result[key] = (
            match.group(1).strip()
            if match
            else None
        )

    if debug:
        missing = [
            key
            for key, value in result.items()
            if value is None
            and key != "url"
        ]

        print(
            f"[debug] {url}",
            file=sys.stderr,
        )

        print(
            f"[debug]   name={result['scraped_name']!r}",
            file=sys.stderr,
        )

        if missing:
            print(
                f"[debug]   missing: {missing}",
                file=sys.stderr,
            )

    return result


# ---------------------------------------------------------------------------
# EXCEL URL LOADER
# ---------------------------------------------------------------------------

def load_urls_from_excel(path: str) -> list:
    """
    Loads fund URLs from the first worksheet, first column,
    starting from row 2.

    Duplicate URLs are removed while preserving order.
    """

    import openpyxl

    wb = openpyxl.load_workbook(
        path,
        data_only=True,
        keep_vba=path.lower().endswith(".xlsm"),
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

        url = str(value).strip()

        if not url:
            continue

        if url in seen:
            continue

        seen.add(url)
        urls.append(url)

    return urls


# ---------------------------------------------------------------------------
# PLAYWRIGHT FETCH
# ---------------------------------------------------------------------------

def fetch_and_parse(
    url: str,
    debug: bool = False,
) -> dict:

    from playwright.sync_api import sync_playwright

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

            factsheet_url = find_factsheet_url(
                page,
                base_url=url,
            )

        finally:
            browser.close()

    result = parse_fund_page(
        text,
        url,
        debug=debug,
    )

    result["holdings"] = []

    # ---------------------------------------------------------------
    # Factsheet holdings
    # ---------------------------------------------------------------

    if factsheet_url:

        try:
            result["holdings"] = (
                fetch_holdings_from_factsheet(
                    factsheet_url,
                    debug=debug,
                )
            )

            if debug:
                print(
                    f"[debug] {url}: extracted "
                    f"{len(result['holdings'])} holdings",
                    file=sys.stderr,
                )

        except Exception as exc:

            if debug:
                print(
                    f"[debug] {url}: factsheet holdings "
                    f"extraction failed "
                    f"({factsheet_url}): {exc}",
                    file=sys.stderr,
                )

    elif debug:

        print(
            f"[debug] {url}: no factsheet URL found",
            file=sys.stderr,
        )

    result["factsheetUrl"] = factsheet_url or "-"

    return result


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
    Best-effort dashboard category.

    If no category can be reasonably determined, return "-".
    """

    name = (
        scraped.get("scraped_name")
        or ""
    ).lower()

    region_hints = [
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

    for hint, category in region_hints:
        if hint in name:
            return category

    asset_class = (
        scraped.get("asset_class")
        or ""
    ).lower()

    if asset_class:
        return ASSET_CLASS_TO_CATEGORY.get(
            asset_class,
            "-",
        )

    return "-"


# ---------------------------------------------------------------------------
# SAFE CONVERSIONS
# ---------------------------------------------------------------------------

def safe_float(value):
    """
    Converts a value to float or returns None.
    """

    if value is None:
        return None

    if isinstance(value, (int, float)):
        return float(value)

    try:
        value = str(value).strip()

        if not value or value == "-":
            return None

        return float(value)

    except Exception:
        return None


def value_or_dash(value):
    """
    Returns the actual value or '-'.
    """

    if value is None:
        return "-"

    value = str(value).strip()

    if not value:
        return "-"

    return value


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------

def main():

    parser = argparse.ArgumentParser()

    parser.add_argument(
        "--urls",
        help=(
            "Excel file (.xlsx/.xlsm) containing "
            "one fund URL per row"
        ),
    )

    parser.add_argument(
        "--input-html",
        help=(
            "Parse one saved fund-page text file "
            "instead of fetching"
        ),
    )

    parser.add_argument(
        "--delay",
        type=float,
        default=1.5,
        help=(
            "Seconds between requests. "
            "Default: 1.5"
        ),
    )

    parser.add_argument(
        "--debug",
        action="store_true",
    )

    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Write scraped_raw.json instead of "
            "changing data.json"
        ),
    )

    args = parser.parse_args()

    # ---------------------------------------------------------------
    # Saved HTML/text parser mode
    # ---------------------------------------------------------------

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
            )
        )

        return

    # ---------------------------------------------------------------
    # Excel required
    # ---------------------------------------------------------------

    if not args.urls:

        print(
            "Provide --urls path/to/Funds_Links.xlsm "
            "or --input-html to test the parser",
            file=sys.stderr,
        )

        sys.exit(1)

    # ---------------------------------------------------------------
    # Load URLs
    # ---------------------------------------------------------------

    urls = load_urls_from_excel(
        args.urls
    )

    print(
        f"Loaded {len(urls)} unique fund URLs "
        f"from {args.urls}"
    )

    if not urls:
        print(
            "No fund URLs found.",
            file=sys.stderr,
        )
        sys.exit(1)

    # ---------------------------------------------------------------
    # Scrape Prudential
    # ---------------------------------------------------------------

    scraped = []

    for index, url in enumerate(
        urls,
        start=1,
    ):

        try:

            parsed = fetch_and_parse(
                url,
                debug=args.debug,
            )

            scraped.append(
                parsed
            )

            print(
                f"[{index}/{len(urls)}] "
                f"{parsed.get('scraped_name') or url} "
                f"-> "
                f"code={parsed.get('code') or '-'} "
                f"risk={parsed.get('risk') or '-'} "
                f"bid={parsed.get('bid') or '-'} "
                f"holdings={len(parsed.get('holdings') or [])}"
            )

        except Exception as exc:

            print(
                f"[{index}/{len(urls)}] "
                f"FAILED {url}: {exc}",
                file=sys.stderr,
            )

        time.sleep(
            args.delay
        )

    # ---------------------------------------------------------------
    # Dry run
    # ---------------------------------------------------------------

    if args.dry_run:

        Path(
            "scraped_raw.json"
        ).write_text(
            json.dumps(
                scraped,
                indent=2,
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        print(
            "\nWrote scraped_raw.json "
            "(dry run - data.json not touched)"
        )

        return

    # ---------------------------------------------------------------
    # Load existing data.json
    # ---------------------------------------------------------------

    if not DATA_PATH.exists():

        print(
            f"ERROR: {DATA_PATH} does not exist.",
            file=sys.stderr,
        )

        sys.exit(1)

    data = json.loads(
        DATA_PATH.read_text(
            encoding="utf-8"
        )
    )

    existing_funds = (
        data
        .get("funds", {})
        .get("funds", [])
    )

    old_by_url = {
        fund.get("sourceUrl"): fund
        for fund in existing_funds
        if fund.get("sourceUrl")
    }

    old_history = data.get(
        "history",
        {},
    )

    old_history_full = data.get(
        "history_full",
        {},
    )

    scraped_by_url = {
        item["url"]: item
        for item in scraped
        if item.get("url")
    }

    # ---------------------------------------------------------------
    # Rebuild from Excel URLs
    # ---------------------------------------------------------------

    new_funds = []

    new_history = {}

    new_history_full = {}

    added = 0

    reused_from_failure = 0

    failed_no_fallback = []

    successful = 0

    # ---------------------------------------------------------------
    # Process each URL in Excel order
    # ---------------------------------------------------------------

    for url in urls:

        scraped_item = (
            scraped_by_url.get(url)
        )

        valid_live_scrape = (
            scraped_item is not None
            and bool(
                scraped_item.get(
                    "scraped_name"
                )
            )
            and safe_float(
                scraped_item.get("bid")
            ) is not None
        )

        # ===========================================================
        # SUCCESSFUL LIVE SCRAPE
        # ===========================================================

        if valid_live_scrape:

            successful += 1

            name = (
                scraped_item["scraped_name"]
                .strip()
            )

            category = guess_category(
                scraped_item
            )

            bid = safe_float(
                scraped_item.get("bid")
            )

            offer = safe_float(
                scraped_item.get("offer")
            )

            # If offer cannot be retrieved, do NOT invent it.
            offer_value = (
                offer
                if offer is not None
                else "-"
            )

            code = value_or_dash(
                scraped_item.get("code")
            )

            risk = value_or_dash(
                scraped_item.get("risk")
            )

            currency = value_or_dash(
                scraped_item.get("currency")
            )

            inception = value_or_dash(
                scraped_item.get("inception")
            )

            cic = value_or_dash(
                scraped_item.get("cic")
            )

            asset_class = value_or_dash(
                scraped_item.get("asset_class")
            )

            # -------------------------------------------------------
            # Fund object
            # -------------------------------------------------------

            fund = {
                "name": name,

                "category": category,

                "currency": currency,

                "effective_date": inception,

                "bid": bid,

                "offer": offer_value,

                "code": code,

                "codeVerified": (
                    code != "-"
                ),

                "riskCategory": risk,

                "cic": cic,

                "assetClass": asset_class,

                "dataSource": "verified-live",

                "sourceUrl": url,

                "factsheetUrl": value_or_dash(
                    scraped_item.get(
                        "factsheetUrl"
                    )
                ),

                # Actual Prudential holdings only.
                # Can legitimately contain fewer than 10.
                "holdings": (
                    scraped_item.get(
                        "holdings"
                    )
                    or []
                ),
            }

            # -------------------------------------------------------
            # Live annualised returns
            # -------------------------------------------------------

            live_returns = {}

            for period, result_key in (
                ("1y", "return_1y"),
                ("3y", "return_3y"),
                ("5y", "return_5y"),
            ):

                value = safe_float(
                    scraped_item.get(
                        result_key
                    )
                )

                if value is not None:
                    live_returns[
                        period
                    ] = value
                else:
                    live_returns[
                        period
                    ] = "-"

            fund[
                "liveReturns"
            ] = live_returns

            # -------------------------------------------------------
            # Preserve existing historical data.
            #
            # The fund URL remains the identity.
            # History is moved to the new scraped name.
            # -------------------------------------------------------

            previous = old_by_url.get(
                url
            )

            if previous:

                previous_name = (
                    previous.get("name")
                )

                if (
                    previous_name
                    and previous_name
                    in old_history
                ):
                    new_history[
                        name
                    ] = old_history[
                        previous_name
                    ]

                if (
                    previous_name
                    and previous_name
                    in old_history_full
                ):
                    new_history_full[
                        name
                    ] = old_history_full[
                        previous_name
                    ]

            # -------------------------------------------------------
            # New fund counter
            # -------------------------------------------------------

            if url not in old_by_url:

                added += 1

                print(
                    f"  + New fund: {name}"
                )

            new_funds.append(
                fund
            )

        # ===========================================================
        # LIVE SCRAPE FAILED — EXISTING URL
        # ===========================================================

        elif url in old_by_url:

            previous = old_by_url[
                url
            ]

            # Preserve previous complete record
            # rather than allowing a temporary network
            # failure to destroy valid data.
            new_funds.append(
                previous
            )

            previous_name = (
                previous.get("name")
            )

            if (
                previous_name
                and previous_name
                in old_history
            ):
                new_history[
                    previous_name
                ] = old_history[
                    previous_name
                ]

            if (
                previous_name
                and previous_name
                in old_history_full
            ):
                new_history_full[
                    previous_name
                ] = old_history_full[
                    previous_name
                ]

            reused_from_failure += 1

            print(
                f"  ! Reused previous data after "
                f"failed scrape: {url}",
                file=sys.stderr,
            )

        # ===========================================================
        # NEW URL — NO LIVE DATA AND NO FALLBACK
        # ===========================================================

        else:

            failed_no_fallback.append(
                url
            )

    # ---------------------------------------------------------------
    # Replace fund collection
    # ---------------------------------------------------------------

    data[
        "funds"
    ][
        "funds"
    ] = new_funds

    data[
        "history"
    ] = new_history

    data[
        "history_full"
    ] = new_history_full

    # ---------------------------------------------------------------
    # Update timestamp
    # ---------------------------------------------------------------

    sgt = timezone(
        timedelta(hours=8)
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

    # ---------------------------------------------------------------
    # Write JSON
    # ---------------------------------------------------------------

    DATA_PATH.write_text(
        json.dumps(
            data,
            ensure_ascii=False,
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )

    # ---------------------------------------------------------------
    # Summary
    # ---------------------------------------------------------------

    print(
        "\n=================================================="
    )

    print(
        "data.json rebuild complete"
    )

    print(
        "=================================================="
    )

    print(
        f"Excel URLs:              {len(urls)}"
    )

    print(
        f"Successful live scrapes: {successful}"
    )

    print(
        f"Funds written:           {len(new_funds)}"
    )

    print(
        f"New funds:               {added}"
    )

    print(
        f"Reused after failure:    {reused_from_failure}"
    )

    print(
        f"Failed with no fallback: {len(failed_no_fallback)}"
    )

    # ---------------------------------------------------------------
    # Important: do NOT claim the counts match when a brand-new
    # URL failed and therefore cannot be written.
    # ---------------------------------------------------------------

    if len(new_funds) == len(urls):

        print(
            "\nFund count matches the Excel URL count."
        )

    else:

        print(
            "\nWARNING:"
        )

        print(
            f"Only {len(new_funds)} of "
            f"{len(urls)} Excel URLs currently have "
            f"usable fund records."
        )

    if failed_no_fallback:

        print(
            "\nURLs with no previous fallback data:",
            file=sys.stderr,
        )

        for failed_url in failed_no_fallback:

            print(
                f"  - {failed_url}",
                file=sys.stderr,
            )

    print()


if __name__ == "__main__":
    main()

