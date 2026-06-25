from playwright.sync_api import sync_playwright
import re
import pandas as pd
import os


# ─────────────────────────────────────────────────────────────────────────
#  University of Delhi special handling
#
#  "University of Delhi" search results on IP India also surface other
#  Delhi-named institutions (most notably Delhi Technological University /
#  DTU). The generic keyword matcher below would treat them as the same
#  institute because they share the word "Delhi" (and "University").
#
#  To fix this we use, *only for University of Delhi*:
#    • a curated POSITIVE list of phrases that genuinely mean Univ. of Delhi
#    • an EXCLUDE list of Delhi institutions that are NOT Univ. of Delhi
#  An excluded institution (e.g. DTU) is never treated as the institute,
#  though it can still appear as a genuine *collaborator* on a joint patent.
# ─────────────────────────────────────────────────────────────────────────

DELHI_UNIVERSITY_ALIASES = {
    "university of delhi",
    "delhi university",
    "univ of delhi",
    "univ. of delhi",
}

DELHI_UNIVERSITY_POSITIVE = [
    "university of delhi",
    "delhi university",
    "univ of delhi",
    "univ. of delhi",
]

# Delhi-named institutions that are NOT the University of Delhi.
DELHI_UNIVERSITY_EXCLUDE = [
    "delhi technological university",
    "delhi technical university",
    "dtu",
    "delhi college of engineering",
    "netaji subhas",
    "nsut",
    "nsit",
    "indraprastha",
    "guru gobind singh",
    "jamia",
    "jawaharlal nehru",
    "jnu",
    "indian institute of technology",
    "iit delhi",
    "iit-delhi",
    "national institute of technology",
    "ambedkar university",
    "south asian university",
    "all india institute of medical",
    "aiims",
    "iiit",
    "indira gandhi",
    "guru tegh bahadur",
]


def build_institute_keywords(institute_name: str) -> list[str]:
    """
    Generate a broad set of keyword variants for the institute so that
    applicants who *are* the institute are never wrongly counted as
    collaborators, even when the name is abbreviated or slightly different.
    """
    name = institute_name.strip()
    name_lower = name.lower()
    keywords = {name_lower}

    # Drop common suffixes to get a bare root
    # e.g. "Aligarh Muslim University" → "aligarh muslim"
    suffixes = [
        " university", " institute of technology", " institute",
        " college", " iit", " nit", " iisc",
    ]
    root = name_lower
    for sfx in suffixes:
        if root.endswith(sfx):
            root = root[: -len(sfx)].strip()
            keywords.add(root)
            break

    # Individual significant words (≥ 4 chars, skip stop-words)
    stop = {"and", "the", "of", "for", "in", "at", "an", "a"}
    words = [w for w in re.split(r"\W+", name_lower) if len(w) >= 4 and w not in stop]
    keywords.update(words)

    # Common abbreviations: first letter of each significant word
    abbrev = "".join(w[0] for w in words)
    if len(abbrev) >= 2:
        keywords.add(abbrev)           # e.g. "amu"
        keywords.add(abbrev + ".")     # e.g. "amu."
        keywords.add(".".join(abbrev)) # e.g. "a.m.u"

    return list(keywords)


def build_matcher(institute_name: str):
    """
    Decide how to match applicants for the searched institute.

    Returns (positive_keywords, exclude_keywords, delhi_mode).

    • For University of Delhi → curated positive list + DTU/other exclusions
      and delhi_mode=True (which also enables the "skip patents with no valid
      University of Delhi applicant" gate in the main loop).
    • For every other institute → the original broad keyword generator with
      no exclusions and delhi_mode=False (original behaviour preserved).
    """
    name_lower = institute_name.strip().lower()

    if name_lower in DELHI_UNIVERSITY_ALIASES or "university of delhi" in name_lower:
        return DELHI_UNIVERSITY_POSITIVE, DELHI_UNIVERSITY_EXCLUDE, True

    return build_institute_keywords(institute_name), [], False


def is_institute_applicant(applicant_name: str,
                           applicant_address: str,
                           positive: list[str],
                           exclude: list[str]) -> bool:
    """
    Return True only if this applicant *is* the institute we searched for.

    EXCLUDE terms take priority: if the applicant text contains an excluded
    term (e.g. 'delhi technological university'), it is NOT our institute,
    even if it also contains a positive term like 'delhi'. This is what keeps
    DTU (and IIT Delhi, Jamia, etc.) from being mistaken for Univ. of Delhi.

    We check the applicant NAME first, because external companies can carry
    the university name in their *address* (e.g. an industry-institute
    collaboration cell) while being genuine external collaborators. The
    address is used only as a last-resort fallback when the name is blank.
    """
    name_lower = applicant_name.strip().lower()
    text = name_lower if name_lower else applicant_address.strip().lower()
    if not text:
        return False

    # Excluded institutions are never the institute itself.
    if any(ex in text for ex in exclude):
        return False

    return any(pos in text for pos in positive)


with sync_playwright() as p:

    browser = p.chromium.launch(headless=False)
    page = browser.new_page()

    page.goto("https://iprsearch.ipindia.gov.in/publicsearch")

    print("\n===================================")
    print("MANUAL STEPS")
    print("===================================")
    print("1. Fill Applicant Name search")
    print("2. Select AND")
    print("3. Enter institute name")
    print("4. Set date range")
    print("5. Solve captcha")
    print("6. Click Search")
    print("===================================\n")

    institute_name = input("Enter institute name exactly as searched: ")
    positive_keywords, exclude_keywords, delhi_mode = build_matcher(institute_name)

    if delhi_mode:
        print("\n*** University of Delhi mode ENABLED ***")
        print("  Positive matches:", positive_keywords)
        print("  Excluded (NOT Univ. of Delhi):", exclude_keywords)
        print("  Patents with no valid University of Delhi applicant will be SKIPPED.\n")
    else:
        print(f"\nMatching keywords built: {positive_keywords}\n")

    input("When results page loads, press ENTER here...")

    page.wait_for_timeout(3000)

    # Try to determine total pages
    total_pages = 1

    try:
        body_text = page.locator("body").inner_text()

        # Example text:
        # Total Document(s): 20
        doc_match = re.search(
            r"Total\s*Document\(s\)\s*:\s*(\d+)",
            body_text,
            re.IGNORECASE
        )

        if doc_match:
            total_docs = int(doc_match.group(1))

            # IP India shows 25 results per page
            total_pages = max(1, (total_docs + 24) // 25)

            print(f"\nTOTAL DOCUMENTS FOUND: {total_docs}")

        else:
            # Fallback to old method
            total_pages_locator = page.locator("#TotalPages")

            if total_pages_locator.count() > 0:
                value = total_pages_locator.first.input_value().strip()

                if value.isdigit():
                    total_pages = int(value)

    except Exception as e:
        print("Page count detection failed:", e)

    print(f"\nTOTAL PAGES FOUND: {total_pages}\n")

    # ── Load any previously saved application numbers so we skip duplicates ──
    output_file = "all_collaborative_patents.csv"
    seen_app_numbers: set[str] = set()

    if os.path.exists(output_file):
        existing_df = pd.read_csv(output_file, dtype=str)
        seen_app_numbers = set(
            existing_df["ApplicationNumber"].dropna().tolist()
        )
        print(f"Loaded {len(seen_app_numbers)} existing records (will skip duplicates).\n")

    collaborative_patents: list[dict] = []
    total_patents_seen = 0
    skipped_no_institute = 0

    for current_page in range(total_pages):

        print(f"\n========== PAGE {current_page + 1}/{total_pages} ==========\n")

        rows = page.locator("#tableData tbody tr")
        row_count = rows.count()

        for row_index in range(row_count):

            try:
                rows = page.locator("#tableData tbody tr")
                current_row = rows.nth(row_index)
                button = current_row.locator("td:nth-child(1) button")

                if button.count() == 0:
                    continue

                app_no = button.inner_text().strip()
                if not app_no:
                    continue

                print(f"Patent {row_index + 1}/{row_count}  [{app_no}]")
                total_patents_seen += 1

                with page.expect_popup() as popup_info:
                    button.click()

                patent_page = popup_info.value
                patent_page.wait_for_load_state()

                page_text = patent_page.locator("body").inner_text()

                # ── Application number (canonical) ───────────────────────
                app_match = re.search(
                    r"Application Number\s+([0-9]+)", page_text
                )
                application_number = app_match.group(1) if app_match else app_no

                # ── Skip if already in CSV ───────────────────────────────
                if application_number in seen_app_numbers:
                    print("  ↩ Already recorded, skipping.")
                    patent_page.close()
                    continue

                # ── Title ────────────────────────────────────────────────
                title_match = re.search(
                    r"Invention Title\s+(.*?)\s+Publication Number",
                    page_text, re.DOTALL,
                )
                title = title_match.group(1).strip() if title_match else ""

                # ── Filing date / year ───────────────────────────────────
                date_match = re.search(
                    r"Application Filing Date\s+([0-9/]+)", page_text
                )
                filing_date = date_match.group(1) if date_match else ""
                year = filing_date[-4:] if filing_date else ""

                # ── IPC classification ───────────────────────────────────
                ipc_match = re.search(
                    r"Classification \(IPC\)\s+(.*?)\s+Inventor",
                    page_text, re.DOTALL,
                )
                ipc = ipc_match.group(1).strip() if ipc_match else ""

                # ── Applicant table ──────────────────────────────────────
                applicant_table = patent_page.locator("table").nth(2)
                applicant_rows = applicant_table.locator("tr")

                applicants: list[dict] = []
                collaborator_names: list[str] = []
                seen_collaborator_names: set[str] = set()  # dedup within patent
                institute_found = False  # did we see a valid Univ. of Delhi applicant?

                for i in range(1, applicant_rows.count()):
                    row = applicant_rows.nth(i)
                    cols = row.locator("td")

                    if cols.count() > 1:
                        applicant_name = cols.nth(0).inner_text().strip()
                        applicant_address = cols.nth(1).inner_text().strip()

                        applicants.append(
                            {"name": applicant_name, "address": applicant_address}
                        )

                        if is_institute_applicant(
                            applicant_name, applicant_address,
                            positive_keywords, exclude_keywords,
                        ):
                            # This applicant IS the institute (e.g. Univ. of Delhi).
                            institute_found = True
                        else:
                            # Everyone else is a potential collaborator
                            # (this correctly includes DTU when a patent is
                            #  a genuine Univ. of Delhi + DTU collaboration).
                            norm = applicant_name.lower().strip()
                            if norm not in seen_collaborator_names:
                                seen_collaborator_names.add(norm)
                                collaborator_names.append(applicant_name)

                # ── Validity gate (University of Delhi only) ─────────────
                # If no genuine University of Delhi applicant exists on this
                # patent (e.g. it is a DTU-only result), skip it entirely.
                if delhi_mode and not institute_found:
                    print("  ✗ Skipped — no valid University of Delhi applicant "
                          "(e.g. DTU-only result).")
                    skipped_no_institute += 1
                    patent_page.close()
                    continue

                # ── Record only if genuinely collaborative ───────────────
                if collaborator_names:
                    record = {
                        "Institute": institute_name,
                        "Year": year,
                        "ApplicationNumber": application_number,
                        "Title": title,
                        "Collaborators": "; ".join(collaborator_names),
                        "ApplicantData": str(applicants),
                        "IPC": ipc,
                    }
                    collaborative_patents.append(record)
                    seen_app_numbers.add(application_number)
                    print(f"  ✅ Collaborative — {collaborator_names}")
                else:
                    print("  ✗ Solo (institute only)")

                patent_page.close()

            except Exception as e:
                print("  ERROR:", e)

        # ── Pagination ───────────────────────────────────────────────────
        if current_page < total_pages - 1:
            next_page_num = current_page + 2  # human-readable page number
            print(f"\nMoving to page {next_page_num}...")
            navigated = False

            # Strategy 1: common DataTables / jQuery pagination selectors
            next_selectors = [
                "button.next",
                "a.next",
                "li.next a",
                "#tableData_next",          # DataTables default id
                ".dataTables_paginate .next",
                "a[data-dt-idx='next']",
                "button[data-page='next']",
                "input[value='Next']",
                "a:has-text('Next')",
                "button:has-text('Next')",
                "a:has-text('›')",
                "a:has-text('»')",
            ]

            for sel in next_selectors:
                try:
                    btn = page.locator(sel).first
                    if btn.count() == 0:
                        continue
                    # Skip if the button/link is visually disabled
                    classes = btn.get_attribute("class") or ""
                    if "disabled" in classes:
                        print(f"  Selector '{sel}' found but is disabled — skipping.")
                        continue
                    btn.click(timeout=5000)
                    page.wait_for_timeout(2000)
                    navigated = True
                    print(f"  ✅ Paginated with selector: '{sel}'")
                    break
                except Exception:
                    continue

            # Strategy 2: find the current active page link and click the next sibling
            if not navigated:
                try:
                    active = page.locator(
                        ".paginate_button.current, li.active > a, "
                        "a.current-page, span.current"
                    ).first
                    if active.count() > 0:
                        # Try clicking the element that comes after the active one
                        page.evaluate("""
                            () => {
                                const active = document.querySelector(
                                    '.paginate_button.current, li.active, '
                                    + 'a.current-page, span.current'
                                );
                                if (active) {
                                    const next = active.nextElementSibling
                                        || active.parentElement?.nextElementSibling;
                                    const clickable = next?.querySelector('a, button') || next;
                                    if (clickable) clickable.click();
                                }
                            }
                        """)
                        page.wait_for_timeout(2000)
                        navigated = True
                        print("  ✅ Paginated via active-sibling JS click.")
                except Exception as e:
                    print(f"  Strategy 2 failed: {e}")

            # Strategy 3: directly set the page input and trigger change
            if not navigated:
                try:
                    page_input = page.locator(
                        "input[name='page'], input#CurrentPage, "
                        "input.current-page, input[type='text'][id*='age']"
                    ).first
                    if page_input.count() > 0:
                        page_input.fill(str(next_page_num))
                        page_input.press("Enter")
                        page.wait_for_timeout(2000)
                        navigated = True
                        print(f"  ✅ Paginated by filling page input with {next_page_num}.")
                except Exception as e:
                    print(f"  Strategy 3 failed: {e}")

            # Strategy 4: ask user to manually go to next page
            if not navigated:
                print("\n  ⚠️  Could not auto-paginate.")
                print(f"  Please manually click to page {next_page_num} in the browser,")
                print("  then press ENTER here to continue...")
                input("  [ENTER when ready] ")
                navigated = True

    # ── Save results ─────────────────────────────────────────────────────
    new_df = pd.DataFrame(collaborative_patents)

    if os.path.exists(output_file):
        existing_df = pd.read_csv(output_file, dtype=str)
        combined = pd.concat([existing_df, new_df], ignore_index=True)
        # Final safety dedup on ApplicationNumber across the entire file
        combined = combined.drop_duplicates(subset=["ApplicationNumber"], keep="first")
    else:
        combined = new_df

    combined.to_csv(output_file, index=False)


    print(f"TOTAL PATENTS SEEN:              {total_patents_seen}")
    if delhi_mode:
        print(f"SKIPPED (no valid Univ. Delhi):  {skipped_no_institute}")
    print(f"NEW COLLABORATIVE PATENTS:       {len(collaborative_patents)}")
    print(f"TOTAL IN FILE:                   {len(combined)}")
    print(f"Saved to: {output_file}")
    print("====================")

    input("\nPress ENTER to close...")
    browser.close()