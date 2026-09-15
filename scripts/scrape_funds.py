#!/usr/bin/env python3
"""
VGrat FMS Dashboard - Prudential Fund Scraper V2

AUTHORITATIVE FUND URL SOURCE
------------------------------
The Excel workbook is the ONLY source used to determine which Prudential
fund pages should be scraped.

Default workbook:
    Funds_Links.xlsm

Expected worksheet:
    FundsURL

Expected structure:
    A1 = header (e.g. FundList)
    A2:A... = Prudential fund URLs

IMPORTANT:
- The scraper is NOT limited to 67 URLs.
- It reads every populated URL below the header, so if rows 69, 70, 71,
  etc. are added later, they are automatically included.
- It also supports Excel cells containing actual hyperlinks.
- Duplicate URLs are removed while preserving Excel order.
- No fund discovery/crawling is performed outside the Excel URL list.

DATA SAFETY
-----------
- A successful scrape updates the corresponding fund.
- A temporary scrape failure NEVER deletes an existing fund record for that
  same Excel URL.
- A new Excel URL that fails to scrape is reported but is not invented as a
  blank/placeholder fund.
- Existing funds whose URL is no longer present in Excel are removed from
  the active fund list, because Excel is authoritative.
- If ZERO URLs scrape successfully, data.json is NOT replaced.
- A backup of the previous data.json is created before a successful write.
- Existing history is preserved; this script does NOT generate synthetic
  history.

Usage:
    pip install playwright openpyxl
    playwright install chromium

    python scripts/scrape_funds_v2.py --urls Funds_Links.xlsm

Optional:
    python scripts/scrape_funds_v2.py --urls Funds_Links.xlsm --dry-run
    python scripts/scrape_funds_v2.py --urls Funds_Links.xlsm --output data.json
"""

from __future__ import annotations

import argparse
import copy
import json
import re
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

from openpyxl import load_workbook
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright


DEFAULT_EXCEL = "Funds_Links.xlsm"
DEFAULT_OUTPUT = "data.json"

MAX_RETRIES = 3
RETRY_DELAY_SECONDS = 2.0
PAGE_TIMEOUT_MS = 45_000
POST_LOAD_WAIT_MS = 2_000

PRUDENTIAL_HOSTS = {
    "prudential.com.sg",
    "www.prudential.com.sg",
}

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/128.0.0.0 Safari/537.36"
)


# ---------------------------------------------------------------------------
# Generic helpers
# ---------------------------------------------------------------------------

def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def clean_text(value: Any) -> str:
    if value is None:
        return ""
    return re.sub(r"\s+", " ", str(value)).strip()


def normalize_name(value: Any) -> str:
    text = clean_text(value).lower()
    text = text.replace("prulink", "prulink")
    text = re.sub(r"[^a-z0-9]+", " ", text)
    text = re.sub(
        r"\b(accumulation|acc|accumulating)\b",
        "accumulation",
        text,
    )
    text = re.sub(
        r"\b(distribution|dist|distributing)\b",
        "distribution",
        text,
    )
    text = re.sub(
        r"\b(decumulation|decum|decumulating)\b",
        "decumulation",
        text,
    )
    return re.sub(r"\s+", " ", text).strip()


def normalize_url(url: str) -> str:
    url = clean_text(url)
    if not url:
        return ""

    # Remove accidental whitespace and trailing slash.
    url = url.strip().rstrip("/")

    # Keep URL case as supplied except hostname normalization.
    parsed = urlparse(url)
    if not parsed.scheme or not parsed.netloc:
        return url

    hostname = parsed.hostname.lower() if parsed.hostname else ""
    netloc = hostname
    if parsed.port:
        netloc += f":{parsed.port}"

    return parsed._replace(netloc=netloc).geturl()


def is_prudential_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
        return (
            parsed.scheme in {"http", "https"}
            and (parsed.hostname or "").lower() in PRUDENTIAL_HOSTS
        )
    except Exception:
        return False


def atomic_write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + ".tmp")

    with temp_path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    temp_path.replace(path)


# ---------------------------------------------------------------------------
# Excel URL loading
# ---------------------------------------------------------------------------

def load_urls_from_excel(excel_path: Path) -> List[Dict[str, Any]]:
    """
    Reads EVERY populated URL from the first worksheet, starting below row 1.

    Supports:
      1. Plain URL text in the cell.
      2. An actual Excel hyperlink in the cell.

    The function deliberately does not stop at row 68 / 67 URLs.
    """

    if not excel_path.exists():
        raise FileNotFoundError(f"Excel file not found: {excel_path}")

    wb = load_workbook(
        filename=excel_path,
        read_only=False,
        data_only=True,
        keep_links=True,
    )

    if not wb.worksheets:
        raise ValueError("Excel workbook contains no worksheets.")

    ws = wb.worksheets[0]

    urls: List[Dict[str, Any]] = []
    seen = set()

    for row_num in range(2, ws.max_row + 1):
        cell = ws.cell(row=row_num, column=1)

        raw_value = clean_text(cell.value)

        hyperlink_target = ""
        if cell.hyperlink is not None:
            hyperlink_target = clean_text(cell.hyperlink.target)

        candidate = hyperlink_target or raw_value
        if not candidate:
            continue

        # Ignore accidental non-URL text.
        if not re.match(r"^https?://", candidate, flags=re.I):
            continue

        url = normalize_url(candidate)

        # Excel is specifically intended to contain Prudential fund pages.
        if not is_prudential_url(url):
            print(
                f"[WARN] Row {row_num}: skipped non-Prudential URL: {candidate}"
            )
            continue

        key = url.lower()

        if key in seen:
            print(f"[INFO] Row {row_num}: duplicate URL skipped: {url}")
            continue

        seen.add(key)

        urls.append(
            {
                "url": url,
                "excelRow": row_num,
                "excelValue": raw_value,
            }
        )

    wb.close()

    return urls


# ---------------------------------------------------------------------------
# Page parsing
# ---------------------------------------------------------------------------

LABELS = {
    "riskClassification": [
        "Risk classification",
        "Risk Classification",
    ],
    "currency": [
        "Currency",
    ],
    "inceptionDate": [
        "Inception date",
        "Inception Date",
    ],
    "fundCode": [
        "Fund code",
        "Fund Code",
    ],
    "cic": [
        "Continuing Investment Charge",
        "Continuing investment charge",
        "CIC",
    ],
    "assetClass": [
        "Asset class",
        "Asset Class",
    ],
}


def get_nonempty_lines(text: str) -> List[str]:
    lines = []
    for raw in text.splitlines():
        value = clean_text(raw)
        if value:
            lines.append(value)
    return lines


def find_label_value(lines: List[str], labels: List[str]) -> Optional[str]:
    """
    Handles common page layouts:
        Label
        Value

    and:
        Label: Value

    and:
        Label Value
    """

    normalized_labels = {
        clean_text(label).lower().rstrip(":")
        for label in labels
    }

    for i, line in enumerate(lines):
        lower = line.lower().strip()

        # Exact label on its own line.
        if lower.rstrip(":") in normalized_labels:
            if i + 1 < len(lines):
                candidate = lines[i + 1]
                if candidate.lower().rstrip(":") not in normalized_labels:
                    return candidate

        # Label: Value
        for label in normalized_labels:
            if lower.startswith(label + ":"):
                value = line.split(":", 1)[1].strip()
                if value:
                    return value

        # Label Value, only when the label is clearly at the start.
        for label in normalized_labels:
            if lower.startswith(label + " "):
                value = line[len(label):].strip(" :")
                if value:
                    return value

    return None


def find_regex_value(
    text: str,
    patterns: List[str],
    flags: int = re.I,
) -> Optional[str]:
    for pattern in patterns:
        match = re.search(pattern, text, flags)
        if match:
            value = clean_text(match.group(1))
            if value:
                return value
    return None


def clean_fund_name(value: str) -> str:
    value = clean_text(value)

    # Remove common browser/page suffixes.
    value = re.sub(r"\s*\|\s*Prudential.*$", "", value, flags=re.I)
    value = re.sub(r"\s*-\s*Prudential.*$", "", value, flags=re.I)

    # Do not allow the title to become the whole page.
    return value.strip(" -|")


def extract_fund_name(title: str, h1: str, url: str) -> str:
    candidates = [clean_fund_name(h1), clean_fund_name(title)]

    for candidate in candidates:
        if not candidate:
            continue

        # Prefer a title that actually looks like a fund.
        if re.search(r"\bPRU(?:Link|Prime)\b", candidate, re.I):
            return candidate

    # Last-resort URL slug.
    slug = urlparse(url).path.rstrip("/").split("/")[-1]
    slug = re.sub(r"^prulink-", "", slug, flags=re.I)
    slug = slug.replace("-", " ").strip()

    if slug:
        return "PRULink " + slug.title()

    return ""


def extract_percentage(text: str, labels: List[str]) -> Optional[str]:
    escaped = "|".join(re.escape(x) for x in labels)

    patterns = [
        rf"(?:{escaped})\s*:?\s*(-?\d+(?:\.\d+)?)\s*%",
        rf"(?:{escaped})[\s\S]{{0,80}}?(-?\d+(?:\.\d+)?)\s*%",
    ]

    return find_regex_value(text, patterns)


def extract_price(text: str, labels: List[str]) -> Optional[float]:
    escaped = "|".join(re.escape(x) for x in labels)

    patterns = [
        rf"(?:{escaped})\s*:?\s*(?:[A-Z]{{3}}\s*)?\$?\s*([\d,]+(?:\.\d+)?)",
        rf"(?:{escaped})[\s\S]{{0,80}}?(?:[A-Z]{{3}}\s*)?\$?\s*([\d,]+(?:\.\d+)?)",
    ]

    raw = find_regex_value(text, patterns)
    if not raw:
        return None

    try:
        return float(raw.replace(",", ""))
    except ValueError:
        return None


def guess_category(fund_name: str, asset_class: Optional[str]) -> str:
    combined = f"{fund_name} {asset_class or ''}".lower()

    # More specific categories first.
    if any(x in combined for x in [
        "money market",
        "cash",
    ]):
        return "Cash / Money Market"

    if any(x in combined for x in [
        "bond",
        "fixed income",
        "income fund",
    ]):
        return "Fixed Income"

    if any(x in combined for x in [
        "balanced",
        "multi asset",
        "multi-asset",
        "managed fund",
        "portfolio",
    ]):
        return "Multi-Asset"

    if any(x in combined for x in [
        "property",
        "reit",
        "real estate",
    ]):
        return "Property / REIT"

    if any(x in combined for x in [
        "asia",
        "asian",
        "emerging",
    ]):
        return "Asia / Emerging Markets"

    if any(x in combined for x in [
        "global",
        "world",
        "international",
    ]):
        return "Global Equity"

    if any(x in combined for x in [
        "america",
        "us equity",
        "u.s.",
        "usa",
    ]):
        return "US Equity"

    if any(x in combined for x in [
        "europe",
        "european",
    ]):
        return "European Equity"

    if any(x in combined for x in [
        "japan",
        "japanese",
    ]):
        return "Japan Equity"

    if any(x in combined for x in [
        "singapore",
        "singapore equity",
    ]):
        return "Singapore Equity"

    if any(x in combined for x in [
        "equity",
        "growth",
        "technology",
        "tech",
        "fund",
    ]):
        return "Equity"

    return "Other"


def parse_fund_page(
    url: str,
    title: str,
    h1: str,
    body_text: str,
) -> Dict[str, Any]:

    text = body_text or ""
    lines = get_nonempty_lines(text)

    fund_name = extract_fund_name(title, h1, url)

    risk = find_label_value(lines, LABELS["riskClassification"])
    currency = find_label_value(lines, LABELS["currency"])
    inception = find_label_value(lines, LABELS["inceptionDate"])
    fund_code = find_label_value(lines, LABELS["fundCode"])
    cic = find_label_value(lines, LABELS["cic"])
    asset_class = find_label_value(lines, LABELS["assetClass"])

    # Regex fallbacks for layouts where the label/value relationship is
    # represented differently in the rendered page.
    risk = risk or find_regex_value(
        text,
        [
            r"Risk\s+classification\s*:?\s*([^\n|]+)",
        ],
    )

    currency = currency or find_regex_value(
        text,
        [
            r"Currency\s*:?\s*([A-Z]{3})\b",
        ],
    )

    inception = inception or find_regex_value(
        text,
        [
            r"Inception\s+date\s*:?\s*([0-9]{1,2}\s+[A-Za-z]+\s+[0-9]{4})",
            r"Inception\s+date\s*:?\s*([0-9]{1,2}[/-][0-9]{1,2}[/-][0-9]{4})",
            r"Inception\s+date\s*:?\s*([A-Za-z]+\s+[0-9]{4})",
        ],
    )

    fund_code = fund_code or find_regex_value(
        text,
        [
            r"Fund\s+code\s*:?\s*([A-Za-z0-9_-]+)",
        ],
    )

    cic = cic or find_regex_value(
        text,
        [
            r"Continuing\s+Investment\s+Charge\s*:?\s*([^\n|]+)",
        ],
    )

    bid = extract_price(
        text,
        ["Bid price", "Bid Price"],
    )

    offer = extract_price(
        text,
        ["Offer price", "Offer Price"],
    )

    one_year = extract_percentage(
        text,
        ["1-year annualised return", "1-year annualised returns", "1 year"],
    )

    three_year = extract_percentage(
        text,
        ["3-year annualised return", "3-year annualised returns", "3 year"],
    )

    five_year = extract_percentage(
        text,
        ["5-year annualised return", "5-year annualised returns", "5 year"],
    )

    return {
        "name": fund_name,
        "url": url,
        "code": fund_code,
        "risk": risk,
        "currency": currency,
        "inceptionDate": inception,
        "cic": cic,
        "assetClass": asset_class,
        "category": guess_category(fund_name, asset_class),
        "bid": bid,
        "offer": offer,
        "returns": {
            "1y": one_year,
            "3y": three_year,
            "5y": five_year,
        },
    }


# ---------------------------------------------------------------------------
