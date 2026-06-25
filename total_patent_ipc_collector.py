"""
total_patent_ipc_collector.py
─────────────────────────────────────────────────────────────────────────────
Collects, for ONE college / institute at a time, from
https://iprsearch.ipindia.gov.in/publicsearch :

    1. The college name you input.
    2. The TOTAL number of patents in your (date-filtered) search results.
    3. The IPC codes of every patent — codes WITHIN one patent are joined by a
       comma ",", and one patent is separated from the NEXT patent by a pipe "|".

       e.g.  A61K,A61P | C07D | H04L,G06F,H04W   →  "A61K,A61P|C07D|H04L,G06F,H04W"

Output (the deliverable):  total_patent_ipc_collector.csv
    Long format — 12 rows per college (re-running a college overwrites its rows):
        Institute, Year, Patents, IPC
      • Row 1     → Year="Total": the total patent count + every patent's IPC.
      • Rows 2-12 → one row per year 2014 … 2024: that year's patent count + IPC.
      A year with 0 patents has Patents=0 and IPC=0.

Input flow is the SAME as the existing Scraper.py:
    You manually fill the search form (Applicant Name AND institute, the
    2014-2024 date range, captcha) and click Search, then press ENTER.

KEY DIFFERENCE vs the old scraper
─────────────────────────────────
The old scraper wrapped each patent in `try/except` and SKIPPED on any error.
When the site crashed while opening a patent or turning the page, a whole page
(~25 patents) could be silently lost.

This collector NEVER silently skips:
    • Opening / reading a patent is retried automatically, then it PAUSES and
      asks you to fix the browser and press ENTER to retry the SAME patent.
    • Turning the page is verified (the first result must actually change). If
      the site crashes, it asks you to navigate manually and waits — it will
      not advance until the next page is really loaded.
"""

from playwright.sync_api import sync_playwright
import re
import os
import pandas as pd

# Filenames / constants -----------------------------------------------------
SUMMARY_FILE = "total_patent_ipc_collector.csv"          # the deliverable
RESULTS_PER_PAGE = 25
YEAR_START, YEAR_END = 2014, 2024
YEARS = [str(y) for y in range(YEAR_START, YEAR_END + 1)]  # "2014" … "2024" (11)


# ── Parsing helpers ────────────────────────────────────────────────────────
def parse_patent_text(page_text: str, fallback_app_no: str):
    """Pull (application_number, year, raw_ipc_text) out of a patent detail page."""
    app_match = re.search(r"Application Number\s+([0-9]+)", page_text)
    application_number = app_match.group(1) if app_match else fallback_app_no

    date_match = re.search(r"Application Filing Date\s+([0-9/]+)", page_text)
    filing_date = date_match.group(1) if date_match else ""
    year = filing_date[-4:] if len(filing_date) >= 4 else ""

    ipc_match = re.search(
        r"Classification \(IPC\)\s+(.*?)\s+Inventor",
        page_text, re.DOTALL,
    )
    ipc_raw = ipc_match.group(1).strip() if ipc_match else ""

    return application_number, year, ipc_raw


def normalize_ipc(raw: str) -> list[str]:
    """
    Turn the raw IPC block of ONE patent into a clean, de-duplicated list of
    codes. IP India lists multiple IPC codes one per line (sometimes comma /
    semicolon separated). We split ONLY on newlines / commas / semicolons so we
    never break a single code that legitimately contains a space (e.g.
    'A61K 31/445').
    """
    if not raw:
        return []
    parts = re.split(r"[\n,;]+", raw)
    codes: list[str] = []
    seen: set[str] = set()
    for part in parts:
        code = re.sub(r"\s+", " ", part).strip()
        if not code:
            continue
        key = code.lower()
        if key not in seen:
            seen.add(key)
            codes.append(code)
    return codes


# ── Summary (deliverable) helpers ──────────────────────────────────────────
SUMMARY_COLUMNS = ["Institute", "Year", "Patents", "IPC"]


def _ipc_string(records: list[dict]) -> str:
    """Join a list of patent records into the IPC string: codes within a patent
    separated by ',', patents separated by '|'."""
    return "|".join(",".join(rec["ipc_codes"]) for rec in records)


def build_institute_rows(institute: str, ordered_records: list[dict]) -> list[dict]:
    """
    Build the 12 output rows for one institute:
      • Row 1     → Year="Total": total patent count + every patent's IPC.
      • Rows 2-12 → one per year 2014…2024: that year's count + IPC.
    A year with no patents gets Patents=0 and IPC="0".
    """
    rows = [{
        "Institute": institute,
        "Year": "Total",
        "Patents": len(ordered_records),
        "IPC": _ipc_string(ordered_records),
    }]

    for year in YEARS:
        year_records = [r for r in ordered_records if r["year"] == year]
        if year_records:
            patents = len(year_records)
            ipc = _ipc_string(year_records)
        else:
            patents = 0
            ipc = "0"
        rows.append({"Institute": institute, "Year": year,
                     "Patents": patents, "IPC": ipc})
    return rows


def write_summary(institute: str, rows: list[dict]):
    """Write/refresh ALL rows for this institute (replacing any previous run)."""
    existing: list[dict] = []

    if os.path.exists(SUMMARY_FILE):
        sdf = pd.read_csv(SUMMARY_FILE, dtype=str).fillna("")
        # Only reuse existing data if it is already in the new 12-row format;
        # an older single-row file is simply replaced.
        if set(SUMMARY_COLUMNS).issubset(sdf.columns):
            sdf = sdf[sdf["Institute"].str.strip().str.lower()
                      != institute.strip().lower()]
            existing = sdf.to_dict("records")

    combined = existing + rows
    pd.DataFrame(combined, columns=SUMMARY_COLUMNS).to_csv(SUMMARY_FILE, index=False)


# ── Robust browser actions (never silently skip) ───────────────────────────
def first_app_fingerprint(page) -> str:
    """Application number shown in the first results row — used to detect that
    pagination really moved us to a new page."""
    try:
        rows = page.locator("#tableData tbody tr")
        if rows.count() == 0:
            return ""
        btn = rows.nth(0).locator("td:nth-child(1) button")
        return btn.inner_text().strip() if btn.count() else ""
    except Exception:
        return ""


def open_patent_with_retry(page, row_index: int, auto_retries: int = 2):
    """
    Open the popup for one results row. Retries automatically, then PAUSES for
    a human and retries the SAME row — it never gives up / never skips.
    Returns (patent_page, app_no) or (None, None) if the row truly has no
    patent button (e.g. a non-data row).
    """
    attempt = 0
    while True:
        try:
            rows = page.locator("#tableData tbody tr")
            current_row = rows.nth(row_index)
            button = current_row.locator("td:nth-child(1) button")

            if button.count() == 0:
                return None, None  # genuinely not a patent row

            app_no = button.inner_text().strip()

            with page.expect_popup(timeout=20000) as popup_info:
                button.click()
            patent_page = popup_info.value
            patent_page.wait_for_load_state(timeout=25000)
            return patent_page, app_no

        except Exception as e:
            attempt += 1
            print(f"     ⚠️  open attempt {attempt} failed: {e}")
            if attempt <= auto_retries:
                page.wait_for_timeout(2500)
                continue
            print("\n     ✋ Could not open this patent automatically.")
            print("        → Close any stray popup tabs, make sure the RESULTS page")
            print("          is showing, then press ENTER to RETRY this SAME patent.")
            print("        (It will NOT be skipped — nothing is lost.)")
            input("        [ENTER to retry] ")
            attempt = 0  # reset and try again


def extract_with_retry(patent_page, fallback_app_no: str, auto_retries: int = 2):
    """Read the patent detail page, retrying (with reload), then human fallback."""
    attempt = 0
    while True:
        try:
            page_text = patent_page.locator("body").inner_text()
            app_no, year, ipc_raw = parse_patent_text(page_text, fallback_app_no)
            if not app_no:
                raise ValueError("application number not found on page")
            return app_no, year, ipc_raw
        except Exception as e:
            attempt += 1
            print(f"     ⚠️  read attempt {attempt} failed: {e}")
            if attempt <= auto_retries:
                try:
                    patent_page.reload()
                    patent_page.wait_for_load_state(timeout=25000)
                except Exception:
                    pass
                continue
            print("\n     ✋ Could not read this patent's details automatically.")
            input("        Adjust the popup if needed, then press ENTER to retry... ")
            attempt = 0


def advance_to_next_page(page, next_page_num: int) -> bool:
    """
    Move to the next results page and VERIFY it actually changed. If the site
    crashes / does nothing, ask the human to navigate manually and wait — we do
    not advance (and therefore never lose a page of ~25 patents) until the next
    page is genuinely loaded.
    """
    before = first_app_fingerprint(page)

    next_selectors = [
        "button.next", "a.next", "li.next a",
        "#tableData_next", ".dataTables_paginate .next",
        "a[data-dt-idx='next']", "button[data-page='next']",
        "input[value='Next']",
        "a:has-text('Next')", "button:has-text('Next')",
        "a:has-text('›')", "a:has-text('»')",
    ]

    def try_auto() -> None:
        # Strategy 1: known "Next" selectors
        for sel in next_selectors:
            try:
                btn = page.locator(sel).first
                if btn.count() == 0:
                    continue
                if "disabled" in (btn.get_attribute("class") or ""):
                    continue
                btn.click(timeout=5000)
                page.wait_for_timeout(2500)
                return
            except Exception:
                continue
        # Strategy 2: click the sibling after the current/active page marker
        try:
            page.evaluate(
                """() => {
                    const active = document.querySelector(
                        '.paginate_button.current, li.active, a.current-page, span.current'
                    );
                    if (active) {
                        const next = active.nextElementSibling
                            || active.parentElement?.nextElementSibling;
                        const clickable = next?.querySelector('a, button') || next;
                        if (clickable) clickable.click();
                    }
                }"""
            )
            page.wait_for_timeout(2500)
        except Exception:
            pass
        # Strategy 3: type the page number into a page input, if present
        try:
            page_input = page.locator(
                "input[name='page'], input#CurrentPage, "
                "input.current-page, input[type='text'][id*='age']"
            ).first
            if page_input.count() > 0:
                page_input.fill(str(next_page_num))
                page_input.press("Enter")
                page.wait_for_timeout(2500)
        except Exception:
            pass

    # First, try automatically.
    try_auto()
    after = first_app_fingerprint(page)
    if after and after != before:
        return True

    # Auto failed — keep asking the human until the page REALLY changes.
    while True:
        print("\n  ⚠️  Could not turn to the next page automatically (the site may")
        print(f"      have crashed). Please manually go to PAGE {next_page_num} in the")
        print("      browser window, wait for the results to load, then press ENTER.")
        input("      [ENTER once page is loaded] ")
        after = first_app_fingerprint(page)
        if after and after != before:
            print("  ✅ Next page detected.")
            return True
        print("  …that still looks like the same page. Let's try once more.")


# ════════════════════════════════════════════════════════════════════════════
def main():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        page = browser.new_page()
        page.goto("https://iprsearch.ipindia.gov.in/publicsearch")

        print("\n===================================")
        print("MANUAL STEPS (same as before)")
        print("===================================")
        print("1. Fill Applicant Name search")
        print("2. Select AND")
        print("3. Enter institute name")
        print("4. Set date range  (2014 – 2024)")
        print("5. Solve captcha")
        print("6. Click Search")
        print("===================================\n")

        institute_name = input("Enter institute name exactly as searched: ").strip()
        input("When the results page has loaded, press ENTER here...")
        page.wait_for_timeout(3000)

        # ── How many pages? ────────────────────────────────────────────────
        total_pages = 1
        total_docs = None
        try:
            body_text = page.locator("body").inner_text()
            doc_match = re.search(
                r"Total\s*Document\(s\)\s*:\s*(\d+)", body_text, re.IGNORECASE
            )
            if doc_match:
                total_docs = int(doc_match.group(1))
                total_pages = max(1, (total_docs + RESULTS_PER_PAGE - 1) // RESULTS_PER_PAGE)
                print(f"\nTOTAL DOCUMENTS REPORTED BY SITE: {total_docs}")
        except Exception as e:
            print("Page count detection failed:", e)
        print(f"TOTAL PAGES TO VISIT: {total_pages}\n")

        # ── Collect patents for this institute (kept in memory) ────────────
        ordered_records: list[dict] = []
        seen_app_numbers: set[str] = set()  # de-dup within this run

        # ── Scrape every page, every row ───────────────────────────────────
        for current_page in range(total_pages):
            print(f"\n========== PAGE {current_page + 1}/{total_pages} ==========\n")

            row_count = page.locator("#tableData tbody tr").count()

            for row_index in range(row_count):
                patent_page, app_no = open_patent_with_retry(page, row_index)
                if patent_page is None:
                    continue  # non-data row, nothing to open

                print(f"Patent {row_index + 1}/{row_count}  [{app_no}]")

                try:
                    application_number, year, ipc_raw = extract_with_retry(
                        patent_page, app_no
                    )
                finally:
                    try:
                        patent_page.close()
                    except Exception:
                        pass

                if application_number in seen_app_numbers:
                    print("   ↩ already recorded — skipping.")
                    continue

                codes = normalize_ipc(ipc_raw)
                seen_app_numbers.add(application_number)
                ordered_records.append(
                    {"app_no": application_number, "year": year, "ipc_codes": codes}
                )

                shown = ",".join(codes) if codes else "(no IPC listed)"
                print(f"   ✅ {year or '????'}  IPC: {shown}")

            # ── Turn the page (verified) ──────────────────────────────────
            if current_page < total_pages - 1:
                advance_to_next_page(page, current_page + 2)

        # ── Build the deliverable (12 rows: Total + each year 2014-2024) ───
        rows = build_institute_rows(institute_name, ordered_records)
        write_summary(institute_name, rows)
        total = len(ordered_records)

        # ── Report ─────────────────────────────────────────────────────────
        print("\n==================== DONE ====================")
        print(f"Institute:              {institute_name}")
        print(f"Patents collected:      {total}")
        if total_docs is not None:
            print(f"Site reported total:    {total_docs}")
            if total != total_docs:
                print(f"  ⚠️  MISMATCH of {abs(total_docs - total)} — some patents may be")
                print("      missing. Re-run the script and redo the same search to")
                print("      collect them (this starts a fresh pass).")
            else:
                print("  ✅ Count matches the site — nothing was lost.")

        print("Year-wise breakdown:")
        for r in rows:
            if r["Year"] != "Total":
                print(f"   {r['Year']}: {r['Patents']}")
        year_sum = sum(int(r["Patents"]) for r in rows if r["Year"] != "Total")
        if year_sum != total:
            print(f"   ⚠️  {total - year_sum} patent(s) had a filing year outside "
                  f"{YEAR_START}-{YEAR_END} (or unreadable) — counted in Total only.")

        print(f"Saved summary to:       {SUMMARY_FILE}")
        print("=============================================")

        input("\nPress ENTER to close the browser...")
        browser.close()


if __name__ == "__main__":
    main()
