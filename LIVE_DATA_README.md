# VGrat FMS — Live Data Setup

This dashboard is a static site (HTML/CSS/JS + `data.json`). It has no
backend of its own, so "live" and "auto-refreshing" data works like this:

## How it actually updates

1. **`scripts/scrape_funds.py`** visits every fund URL listed in
   `Funds_Links.xlsm` (one row per fund) and pulls the real fund code,
   risk classification, currency, bid/offer price, and 1/3/5-year returns
   from each page. It then rewrites `data.json`.
2. **`.github/workflows/daily-refresh.yml`** runs that script automatically
   every day at 8:00am Singapore time, using GitHub's own servers — you
   don't need to host or run anything yourself. It commits the updated
   `data.json` straight back into the repo.
3. The dashboard is just reading `data.json` like any other file, so once
   GitHub Actions updates it, the live site reflects it on next load.
4. The **Refresh** button and the "Updated ⟨date + time⟩" text in the
   sidebar re-read `data.json` fresh (with cache-busting) — if you're on
   the page when 8am passes, it'll also silently re-pull once per day.

## Why not fetch Prudential's site directly from the browser?

Browsers enforce CORS (Cross-Origin Resource Sharing) — Prudential's
servers don't grant permission for another website's JavaScript to read
their pages directly, and that's not something we can configure from our
side. Scripts running outside a browser (like the GitHub Action here)
aren't subject to CORS at all, which is why the scraper runs there instead.

## One-time setup (you only need to do this once)

1. Push this project to a GitHub repository.
2. In the repo, go to **Settings → Pages** and enable GitHub Pages,
   serving from the branch/folder this project lives in. That gives you a
   free live URL for the dashboard.
3. GitHub Actions is enabled by default — the workflow file is already
   included (`.github/workflows/daily-refresh.yml`), so the daily 8am
   refresh starts working as soon as the repo exists. You can also trigger
   it manually any time from the repo's **Actions** tab
   ("Daily fund data refresh" → **Run workflow**).

## Running the scraper yourself (optional, e.g. to test it)

```bash
pip install playwright openpyxl
playwright install chromium
python scripts/scrape_funds.py --urls Funds_Links.xlsm
```

Add `--dry-run` to see what it would extract without touching `data.json`,
or `--debug` to see which fields it couldn't find on a given page.

## What's real vs. what's still simulated

- **Real (from Prudential, once the scraper has run)**: fund code, risk
  classification, currency, bid/offer price, 1/3/5-year returns.
- **Still simulated**: the day-by-day price history used for the
  performance charts, and top-10 holdings for funds the scraper hasn't
  covered yet — Prudential doesn't publish a daily price feed anywhere
  that allows automated access, so this remains a reasonable approximation
  rather than genuine tick-by-tick history.
- Each fund in `data.json` has a `dataSource` field: `"verified-live"`
  once the scraper has successfully pulled it, `"pending-verification"`
  otherwise.
