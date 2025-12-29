import os
import re
import asyncio
from urllib.parse import urljoin, urlparse
from datetime import datetime
from playwright.async_api import async_playwright

# ================= CONFIG =================

BASE_PAGE_URL = "https://www.justice.gov/epstein/doj-disclosures/data-set-8-files?page={}"
BASE_SITE = "https://www.justice.gov"
END_PAGE = 220

OUT_DIR = "epstein_dataset_8_pdfs"
LOG_FILE = "missing_pull.log"

SLEEP_BETWEEN_DOWNLOADS = 1.2
SLEEP_BETWEEN_PAGES = 2.5

# =========================================


def log(msg):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def get_local_files():
    """
    Return set of local PDF filenames.
    """
    if not os.path.exists(OUT_DIR):
        return set()
    return {f for f in os.listdir(OUT_DIR) if f.lower().endswith(".pdf")}


async def create_fresh_context(browser):
    """
    Force DOJ robot challenge by creating a fresh browser context.
    """
    context = await browser.new_context()
    page = await context.new_page()

    log("NEW CONTEXT ΓÇö opening page 1 for DOJ re-auth")
    await page.goto(BASE_PAGE_URL.format(1), wait_until="load")

    print("\n=== AUTH REQUIRED ===")
    print("If you see 'I am not a robot', CLICK IT.")
    print("Wait until the dataset list is visible.")
    input("Press ENTER here AFTER the list is visible...\n")

    return context, page


async def fetch_pdf(context, url, referer):
    return await context.request.get(
        url,
        timeout=120000,
        headers={
            "Referer": referer,
            "Accept": "application/pdf,*/*",
        },
    )


async def main():
    os.makedirs(OUT_DIR, exist_ok=True)

    local_files = get_local_files()
    log(f"Local files detected: {len(local_files)}")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False, slow_mo=25)
        context, page = await create_fresh_context(browser)

        # ================= BUILD ONLINE LIST =================
        online_files = {}  # filename -> source page

        for page_num in range(1, END_PAGE + 1):
            page_url = BASE_PAGE_URL.format(page_num)
            log(f"Scanning page {page_num}")
            await page.goto(page_url, wait_until="networkidle")

            hrefs = await page.eval_on_selector_all(
                "a[href]",
                "els => els.map(e => e.getAttribute('href'))"
            )

            for href in hrefs:
                if not href:
                    continue
                full_url = urljoin(BASE_SITE, href)
                if "/epstein/files/" in full_url.lower() and full_url.lower().endswith(".pdf"):
                    fname = os.path.basename(urlparse(full_url).path)
                    online_files[fname] = (full_url, page_url)

            await asyncio.sleep(SLEEP_BETWEEN_PAGES)

        log(f"Total PDFs listed online: {len(online_files)}")

        missing = sorted(set(online_files.keys()) - local_files)

        log(f"Missing PDFs detected: {len(missing)}")
        if not missing:
            log("No missing files ΓÇö dataset is complete")
            await browser.close()
            return

        # ================= DOWNLOAD MISSING =================
        for fname in missing:
            pdf_url, referer = online_files[fname]
            out_path = os.path.join(OUT_DIR, fname)

            log(f"DOWNLOAD MISSING {fname}")

            retries = 0
            while True:
                resp = await fetch_pdf(context, pdf_url, referer)

                if resp.status == 200:
                    with open(out_path, "wb") as f:
                        f.write(await resp.body())
                    log(f"DONE {fname}")
                    await asyncio.sleep(SLEEP_BETWEEN_DOWNLOADS)
                    break

                if resp.status == 401:
                    retries += 1
                    log(f"401 for {fname} ΓÇö re-auth required (attempt {retries})")
                    await context.close()
                    context, page = await create_fresh_context(browser)
                    continue

                log(f"ERROR HTTP {resp.status} for {fname}")
                break

        await browser.close()
        log("MISSING FILE PASS COMPLETE")


if __name__ == "__main__":
    asyncio.run(main())
