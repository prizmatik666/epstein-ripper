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
LOG_FILE = "download.log"
STATE_FILE = "resume_state.txt"

# Throttling (important to avoid lockouts)
SLEEP_BETWEEN_DOWNLOADS = 1.0   # seconds
SLEEP_BETWEEN_PAGES = 3.0       # seconds

# =========================================


def log(msg):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def find_last_downloaded_number():
    """
    Find highest EFTA#####.pdf already downloaded.
    """
    if not os.path.exists(OUT_DIR):
        return None

    pattern = re.compile(r"EFTA0*(\d+)\.pdf", re.IGNORECASE)
    max_num = None

    for fname in os.listdir(OUT_DIR):
        m = pattern.match(fname)
        if m:
            num = int(m.group(1))
            if max_num is None or num > max_num:
                max_num = num

    return max_num


def load_resume_page():
    """
    Load last known page from resume_state.txt.
    """
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f:
                page = int(f.read().strip())
                if 1 <= page <= END_PAGE:
                    return page
        except Exception:
            pass
    return None


def save_resume_page(page_num):
    with open(STATE_FILE, "w") as f:
        f.write(str(page_num))


async def create_fresh_context(browser):
    """
    Create a brand-new browser context to force DOJ robot challenge.
    """
    context = await browser.new_context()
    page = await context.new_page()

    log("NEW CONTEXT ΓÇö opening page 1 for DOJ re-auth")
    await page.goto(BASE_PAGE_URL.format(1), wait_until="load")

    print("\n=== AUTH REQUIRED ===")
    print("If you see 'I am not a robot', CLICK IT in the browser.")
    print("Wait until the dataset file list is visible.")
    input("Press ENTER here AFTER the list is visible...\n")

    return context, page


async def fetch_pdf(context, url, referer):
    """
    Download PDF using authenticated browser request context.
    """
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

    last_num = find_last_downloaded_number()
    resume_page = load_resume_page()

    if last_num:
        log(f"Detected last PDF: EFTA{last_num:08d}.pdf")
    else:
        log("No existing PDFs found")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False, slow_mo=25)

        # Always start with a fresh context
        context, page = await create_fresh_context(browser)

        # Determine starting page
        if resume_page:
            start_page = resume_page
            log(f"Resuming directly from saved page {start_page}")
        else:
            log("Locating page of last downloaded file (one-time scan)")
            start_page = 1

            if last_num:
                for pnum in range(1, END_PAGE + 1):
                    await page.goto(BASE_PAGE_URL.format(pnum), wait_until="networkidle")

                    hrefs = await page.eval_on_selector_all(
                        "a[href]",
                        "els => els.map(e => e.getAttribute('href'))"
                    )

                    for href in hrefs:
                        if href and f"EFTA{last_num:08d}.pdf" in href:
                            start_page = pnum
                            save_resume_page(pnum)
                            log(f"Last file found on page {pnum}")
                            break
                    else:
                        continue
                    break

        completed = 0

        # ================= MAIN LOOP =================
        for page_num in range(start_page, END_PAGE + 1):
            save_resume_page(page_num)
            page_url = BASE_PAGE_URL.format(page_num)
            log(f"Scanning page {page_num}")

            await page.goto(page_url, wait_until="networkidle")

            hrefs = await page.eval_on_selector_all(
                "a[href]",
                "els => els.map(e => e.getAttribute('href'))"
            )

            pdf_links = []
            for href in hrefs:
                if not href:
                    continue
                full_url = urljoin(BASE_SITE, href)
                if "/epstein/files/" in full_url.lower() and full_url.lower().endswith(".pdf"):
                    pdf_links.append(full_url)

            log(f"Found {len(pdf_links)} PDFs on page {page_num}")

            for pdf_url in pdf_links:
                filename = os.path.basename(urlparse(pdf_url).path)

                m = re.match(r"EFTA0*(\d+)\.pdf", filename, re.IGNORECASE)
                if not m:
                    continue

                file_num = int(m.group(1))
                if last_num and file_num <= last_num:
                    continue

                out_path = os.path.join(OUT_DIR, filename)
                log(f"DOWNLOAD {filename}")

                resp = await fetch_pdf(context, pdf_url, page_url)

                if resp.status == 200:
                    with open(out_path, "wb") as f:
                        f.write(await resp.body())
                    completed += 1
                    last_num = file_num
                    log(f"DONE ({completed}) {filename}")
                    await asyncio.sleep(SLEEP_BETWEEN_DOWNLOADS)
                    continue

                if resp.status == 401:
                    log(f"401 for {filename} ΓÇö DOJ auth expired")
                    await context.close()
                    context, page = await create_fresh_context(browser)
                    continue

                log(f"ERROR HTTP {resp.status} for {filename}")

            await asyncio.sleep(SLEEP_BETWEEN_PAGES)

        await browser.close()
        log(f"FINISHED : {completed} new PDFs downloaded")


if __name__ == "__main__":
    asyncio.run(main())
