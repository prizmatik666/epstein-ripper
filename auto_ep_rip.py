!/usr/bin/env python3
#
#     Prizm presents
#   ▄████████    ▄███████▄    ▄████████  ▄█     ▄███████▄
#  ███    ███   ███    ███   ███    ███ ███    ███    ███
#  ███    █▀    ███    ███   ███    ███ ███▌   ███    ███
# ▄███▄▄▄       ███    ███  ▄███▄▄▄▄██▀ ███▌   ███    ███
#▀▀███▀▀▀      ▀█████████▀  ▀▀███▀▀▀▀▀   ███▌ ▀█████████▀
#  ███    █▄    ███        ▀███████████ ███    ███
#  ███    ███   ███ STEIN  - ███    ███ ███    ███ PER
#  ██████████  ▄████▀        ███    ███ █▀    ▄████▀
#   [ AUTOMATIC ]                       ███    ███ version 3
#                        A Prizmatik Underground Production
# ==========================================================
# Epstein DOJ Dataset Tools
# Author: Prizm (Prizmatik Underground)
# Repository:
# https://github.com/prizmatik666/epstein-ripper
#
# Support Development:
# PayPal: https://www.paypal.com/ncp/payment/VVDAXZGKPQZKW
# Email: prizmatikug@gmail.com
#
#=======================================================#
# UPDATED VERSION: 3/2/2026 [auto_ep_rip.py]              #
#_______________________________________________________#
# automated the page-context approval, age button click,#
# not-robot button checks, session poison re-auth. :) :)#
# HUGE QUALITY OF LIFE IMPROVEMENT!! Hope it helps every#
# body out!!!!                                          #
#=======================================================#
# UPDATED VERSION: 2/27/2026                            #
#_______________________________________________________#
# found a new error during download. when it runs into
# giant files (this one was ~512mb) . It returns an error
# Cannot create a string longer than 0x1fffffe8 characters
# added a check for this and changed the way THOSE files
# are downloaded.
# ======================================================#
# UPDATED VERSION: 2/25/2026                            #
#-------------------------------------------------------#
#  THIS VERSION HAS A FIX FOR THE HTML-PAGE AS PDF PROBLEM
# NOTE (Session-Poison Protection):
# DOJ sometimes returns an HTML gate/age-verify/bot-wall page with HTTP 200 instead of a real PDF.
# This ripper prevents corrupted saves by downloading to a temporary ".part" file, then validating
# the PDF signature ("%PDF-") before committing the final ".pdf". If the response is not a PDF,
# it triggers a SESSION_POISON pause: prints a loud alert + bell, waits for user confirmation,
# rebuilds a fresh Playwright browser context (re-auth), and retries the same file. Normal HTTP
# errors like 404 are logged and skipped without forcing a context refresh.
# BUT- a 404 error during download would suggest files that DOJ removed
# since your datasets index file was built - sneaky sneaky
#-------------------------------------------------------#
import os
import re
import json
import time
import hashlib
import argparse
import asyncio
import sys
from urllib.parse import urljoin, urlparse
from datetime import datetime
from typing import Dict, Any, List, Tuple, Optional

import aiohttp
from playwright.async_api import async_playwright
from playwright.async_api import TimeoutError as PWTimeoutError

# ================= CONFIG =================

BASE_SITE = "https://www.justice.gov"

# DOJ currently has datasets 1ΓÇô12 (adjust later if they add more)
DATASET_RANGE = range(1, 13)

DATASETS = {
    n: {
        "base_url": f"https://www.justice.gov/epstein/doj-disclosures/data-set-{n}-files?page={{}}",
        "out_dir": f"data{n}",
        "state_file": f"resume_data{n}.txt",
        "index_file": f"index_data{n}.json",
    }
    for n in DATASET_RANGE
}

LOG_FILE = "download.log"

# Throttling
SLEEP_BETWEEN_DOWNLOADS = 0.75
SLEEP_BETWEEN_PAGES = 0.5

# Stop conditions
MAX_PAGES_WITH_NO_NEW_PDFS = 30
MAX_PAGES_HARD_CAP = 200000

# Retry behavior (for real download failures; session-poison does NOT consume retries)
MAX_DOWNLOAD_RETRIES = 35

# "Unicorn" PDFs: avoid Playwright resp.body() protocol limit by streaming
UNICORN_SIZE_BYTES = 100 * 1024 * 1024  # 100MB threshold

# --- Age gate selectors (from DOJ HTML) ---
AGE_GATE_BLOCK = "#age-verify-block"
AGE_YES_BTN = "#age-button-yes"

# --- Abuse deterrent / robot gate selectors ---
# Page includes: <input type="button" class="usa-button" value="I am not a robot" onclick="reauth();">
ROBOT_BTN = "input.usa-button[value='I am not a robot'], input.usa-button[onclick*='reauth']"

# --- Dataset list selector ---
# This block contains the file list (<a href="...EFTA....pdf">)
DATASET_LIST_PDF_LINKS = "div.block-usdoj-external-files-block a[href$='.pdf']"

# How long to wait for the dataset list after opening auth page
AUTH_WAIT_SECONDS = 600  # 10 minutes: allows manual captcha if it appears

# --- Auth stability sleeps (your requested staging) ---
# spawn browser / sleep / button / sleep / list detect / sleep -> start download
AUTH_SLEEP_AFTER_GOTO = 1.5
AUTH_SLEEP_AFTER_ROBOT_CLICK = 1.0
AUTH_SLEEP_AFTER_AGE_CLICK = 0.6
AUTH_SLEEP_AFTER_LIST_VISIBLE = 0.8

# --- Warmup / keep-window logic ---
# Sometimes closing the auth page too early breaks first downloads.
# We'll "warm up" the session via context.request to verify we can see PDF links,
# and optionally keep the window open briefly after auth.
AUTH_WARMUP_ENABLED = True
AUTH_WARMUP_RETRIES = 3
AUTH_WARMUP_SLEEP = 0.75

# Keep the auth page open for a short time after warmup (helps some flaky sessions).
KEEP_AUTH_PAGE_OPEN_SECONDS = 2.0  # set 0 to disable
# If True, we will close the auth page automatically after warmup + keep-open delay.
CLOSE_AUTH_PAGE_AFTER_AUTH = True

# =========================================


def log(msg: str) -> None:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def sha256_file(path: str, chunk_size: int = 1024 * 1024) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(chunk_size)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def safe_json_load(path: str) -> Dict[str, Any]:
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        try:
            bad = path + ".corrupt"
            os.replace(path, bad)
            log(f"WARNING: Index file corrupted, moved to {bad} and starting fresh.")
        except Exception:
            log("WARNING: Index file corrupted and could not be moved. Starting fresh.")
        return {}


def safe_json_save(path: str, data: Dict[str, Any]) -> None:
    """
    Atomic-ish JSON save.

    Hardened to avoid rare crash:
      FileNotFoundError: '...json.tmp' -> '...json'

    Causes we defend against:
      - directory missing (ensure it exists)
      - transient FS weirdness (retry replace a couple times)
    """
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)

    tmp = path + ".tmp"

    # Write tmp
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, sort_keys=True)
        f.flush()
        try:
            os.fsync(f.fileno())
        except Exception:
            pass

    # Replace with small retry loop (in case something external deletes tmp)
    for attempt in range(3):
        try:
            os.replace(tmp, path)
            return
        except FileNotFoundError:
            # tmp missing at replace time; try to re-write once
            if attempt < 2:
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=2, sort_keys=True)
                    f.flush()
                    try:
                        os.fsync(f.fileno())
                    except Exception:
                        pass
                time.sleep(0.05)
                continue
            raise


def load_resume_page(state_file: str) -> Optional[int]:
    if os.path.exists(state_file):
        try:
            with open(state_file, "r", encoding="utf-8") as f:
                n = int(f.read().strip())
                return n if n >= 1 else None
        except Exception:
            return None
    return None


def save_resume_page(state_file: str, page_num: int) -> None:
    with open(state_file, "w", encoding="utf-8") as f:
        f.write(str(page_num))


def ask_datasets_interactive() -> List[int]:
    available = sorted(DATASETS.keys())
    print("\nAvailable datasets:")
    print(",".join(str(d) for d in available))

    raw = input(
        "\nEnter dataset numbers separated by commas (example: 1,3,5) "
        "or a range (example: 1-11): "
    ).strip()

    selected = set()

    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue

        if "-" in part:
            try:
                start, end = map(int, part.split("-", 1))
                if start > end:
                    start, end = end, start
                for n in range(start, end + 1):
                    if n in DATASETS:
                        selected.add(n)
            except ValueError:
                pass
        else:
            try:
                n = int(part)
                if n in DATASETS:
                    selected.add(n)
            except ValueError:
                pass

    if not selected:
        print("No valid datasets selected. Exiting.")
        raise SystemExit(1)

    return sorted(selected)


def ask_mode_interactive() -> str:
    print("\nMode options:")
    print("  sync     = scan + download (recommended)")
    print("  scan     = only scan and update index (no downloads)")
    print("  download = only download missing from index (no scanning)")

    raw = input("\nChoose mode [sync]: ").strip().lower()
    return raw if raw in {"sync", "scan", "download"} else "sync"


async def ensure_robot_verified(page, dataset_id: int) -> bool:
    """
    Auto-click DOJ abuse-deterrent robot page if present.
    Safe to call repeatedly.
    Returns True if it clicked.
    """
    try:
        btn = page.locator(ROBOT_BTN).first
        if await btn.is_visible(timeout=250):
            log(f"[DS {dataset_id}] [auth] Robot gate detected -> clicking 'I am not a robot'")
            await btn.click(timeout=8000)
            await asyncio.sleep(AUTH_SLEEP_AFTER_ROBOT_CLICK)
            # After reauth(), page reloads. Give it a moment.
            try:
                await page.wait_for_load_state("domcontentloaded", timeout=20000)
            except PWTimeoutError:
                pass
            return True
    except PWTimeoutError:
        return False
    except Exception:
        return False
    return False


async def ensure_age_verified(page, dataset_id: int) -> bool:
    """
    Auto-click DOJ age gate Yes if present.
    Safe to call repeatedly.
    Returns True if it clicked.
    """
    try:
        gate = page.locator(AGE_GATE_BLOCK).first
        if await gate.is_visible(timeout=250):
            log(f"[DS {dataset_id}] [auth] Age gate detected -> clicking YES")
            await page.locator(AGE_YES_BTN).click(timeout=8000)
            await asyncio.sleep(AUTH_SLEEP_AFTER_AGE_CLICK)
            return True
    except PWTimeoutError:
        return False
    except Exception:
        return False
    return False


async def wait_for_dataset_list(page, dataset_id: int, timeout_s: int = AUTH_WAIT_SECONDS) -> None:
    """
    Wait until the dataset file list is visible (PDF links appear).
    Handles BOTH:
      - robot gate page (reauth button)
      - age verify gate
    """
    log(f"[DS {dataset_id}] Waiting for dataset list to become visible (hands-free)...")
    deadline = time.time() + timeout_s

    while True:
        # Clear gates if they appear
        await ensure_robot_verified(page, dataset_id=dataset_id)
        await ensure_age_verified(page, dataset_id=dataset_id)

        try:
            count = await page.locator(DATASET_LIST_PDF_LINKS).count()
            if count and count > 0:
                log(f"[DS {dataset_id}] Dataset list visible (pdf links found: {count})")
                await asyncio.sleep(AUTH_SLEEP_AFTER_LIST_VISIBLE)
                return
        except Exception:
            pass

        if time.time() > deadline:
            raise TimeoutError(
                f"Timed out waiting for dataset list (>{timeout_s}s). "
                f"If DOJ shows a captcha/robot check that blocks automation, solve it in the browser window."
            )

        await asyncio.sleep(0.5)


async def warmup_session(context, first_page_url: str, dataset_id: int) -> None:
    """
    Verify the auth session is actually usable BEFORE we proceed/close auth window.
    We do a context.request.get() and ensure it contains PDF links.
    """
    if not AUTH_WARMUP_ENABLED:
        return

    for attempt in range(1, AUTH_WARMUP_RETRIES + 1):
        try:
            r = await context.request.get(first_page_url, timeout=60000, headers={"Accept": "text/html,*/*"})
            if r.status != 200:
                log(f"[DS {dataset_id}] [auth] Warmup attempt {attempt}: HTTP {r.status}")
                await asyncio.sleep(AUTH_WARMUP_SLEEP)
                continue

            txt = await r.text()
            # Cheap-but-effective: look for DOJ epstein file link pattern.
            if ("/epstein/files/" in txt) and (".pdf" in txt):
                log(f"[DS {dataset_id}] [auth] Warmup OK (session can see PDF links)")
                return

            # If we got robot/age gate HTML, we'll just retry after a short delay.
            log(f"[DS {dataset_id}] [auth] Warmup attempt {attempt}: still gated (no PDF links in HTML)")
            await asyncio.sleep(AUTH_WARMUP_SLEEP)
            continue

        except Exception as e:
            log(f"[DS {dataset_id}] [auth] Warmup attempt {attempt}: ERROR {type(e).__name__}: {e}")
            await asyncio.sleep(AUTH_WARMUP_SLEEP)

    log(f"[DS {dataset_id}] [auth] Warmup did not confirm PDF visibility, continuing anyway (may still work).")


async def create_fresh_context(browser, first_page_url: str, dataset_id: int):
    """
    Hands-free auth context:
      - opens page
      - staged sleeps (stability)
      - auto-clicks robot gate if present
      - auto-clicks age gate YES if present
      - waits for dataset list to appear
      - warms up session via context.request
      - (optional) keeps auth page open briefly
      - (optional) closes auth page
      - returns (context, auth_page_or_none)
    """
    context = await browser.new_context()
    page = await context.new_page()

    log(f"[DS {dataset_id}] NEW CONTEXT ΓÇö opening dataset page for DOJ auth")
    await page.goto(first_page_url, wait_until="domcontentloaded")
    await asyncio.sleep(AUTH_SLEEP_AFTER_GOTO)

    # Clear gates + wait for list
    await ensure_robot_verified(page, dataset_id=dataset_id)
    await ensure_age_verified(page, dataset_id=dataset_id)
    await wait_for_dataset_list(page, dataset_id=dataset_id, timeout_s=AUTH_WAIT_SECONDS)

    # Warmup the session so we don't close too early and break first downloads
    await warmup_session(context, first_page_url=first_page_url, dataset_id=dataset_id)

    if KEEP_AUTH_PAGE_OPEN_SECONDS and KEEP_AUTH_PAGE_OPEN_SECONDS > 0:
        log(f"[DS {dataset_id}] [auth] Keeping auth window open for {KEEP_AUTH_PAGE_OPEN_SECONDS:.1f}s (stability)")
        await asyncio.sleep(KEEP_AUTH_PAGE_OPEN_SECONDS)

    if CLOSE_AUTH_PAGE_AFTER_AUTH:
        try:
            await page.close()
            page = None
            log(f"[DS {dataset_id}] [auth] Auth window closed (session should be ready)")
        except Exception:
            pass

    return context, page


async def fetch_pdf(context, url: str, referer: str):
    return await context.request.get(
        url,
        timeout=180000,
        headers={
            "Referer": referer,
            "Accept": "application/pdf,*/*",
        },
    )


def is_valid_epstein_pdf_url(full_url: str) -> bool:
    u = full_url.lower()
    return ("/epstein/files/" in u) and u.endswith(".pdf")


def extract_file_num(filename: str) -> Optional[int]:
    m = re.match(r"EFTA0*(\d+)\.pdf$", filename, re.IGNORECASE)
    if not m:
        return None
    return int(m.group(1))


def index_path_for_dataset(out_dir: str, index_file: str) -> str:
    return os.path.join(out_dir, index_file)


def init_index_structure(idx: Dict[str, Any], dataset_id: int) -> Dict[str, Any]:
    if not idx:
        return {
            "meta": {
                "dataset": dataset_id,
                "created_at": datetime.now().isoformat(timespec="seconds"),
                "last_scan_at": None,
                "last_scan_page": 0,
                "version": 2,
            },
            "files": {}
        }
    idx.setdefault("meta", {})
    idx.setdefault("files", {})
    idx["meta"].setdefault("dataset", dataset_id)
    idx["meta"].setdefault("version", 2)
    idx["meta"].setdefault("created_at", datetime.now().isoformat(timespec="seconds"))
    idx["meta"].setdefault("last_scan_at", None)
    idx["meta"].setdefault("last_scan_page", 0)
    return idx


def upsert_index_entry(idx: Dict[str, Any], filename: str, url: str, page_num: int) -> bool:
    files = idx["files"]
    now = datetime.now().isoformat(timespec="seconds")

    if filename not in files:
        files[filename] = {
            "url": url,
            "first_seen": now,
            "last_seen": now,
            "page": page_num,
            "downloaded": False,
            "downloaded_at": None,
            "sha256": None,
            "bytes": None,
            "attempts": 0,
            "last_error": None,
        }
        return True

    files[filename]["url"] = url
    files[filename]["last_seen"] = now
    files[filename]["page"] = page_num
    return False


def needs_download(out_path: str, entry: Dict[str, Any]) -> bool:
    if entry.get("downloaded") and os.path.exists(out_path):
        return False
    if os.path.exists(out_path) and not entry.get("downloaded"):
        return False
    return True


def bytes_look_like_pdf(b: bytes) -> bool:
    return b.startswith(b"%PDF-")


def loud_session_poison_alert(dataset_id: int, filename: str):
    msg = f"[DS {dataset_id}] ERROR! NON-PDF RESPONSE (HTML / GATE) for {filename}"
    print("\n" + "=" * 78)
    for _ in range(10):
        print(msg)
    print("=" * 78 + "\n")
    sys.stdout.write("\a" * 10)
    sys.stdout.flush()


def build_cookie_header(cookies: List[Dict[str, Any]]) -> str:
    parts = []
    for c in cookies:
        name = c.get("name")
        value = c.get("value")
        if name and value is not None:
            parts.append(f"{name}={value}")
    return "; ".join(parts)


async def stream_download_via_aiohttp(
    context,
    url: str,
    referer: str,
    out_path: str,
    part_path: str,
) -> Tuple[bool, str]:
    try:
        cookies = await context.cookies(url)
    except Exception:
        cookies = await context.cookies()

    cookie_header = build_cookie_header(cookies)

    headers = {
        "Referer": referer,
        "Accept": "application/pdf,*/*",
    }
    if cookie_header:
        headers["Cookie"] = cookie_header

    timeout = aiohttp.ClientTimeout(total=None, sock_connect=60, sock_read=300)

    if os.path.exists(part_path):
        try:
            os.remove(part_path)
        except Exception:
            pass

    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, headers=headers, allow_redirects=True) as r:
                if r.status != 200:
                    return False, f"HTTP {r.status}"

                first = await r.content.read(16)
                if not first or not bytes_look_like_pdf(first):
                    try:
                        if os.path.exists(part_path):
                            os.remove(part_path)
                    except Exception:
                        pass
                    return False, "SESSION_POISON"

                with open(part_path, "wb") as f:
                    f.write(first)
                    while True:
                        chunk = await r.content.read(1024 * 1024)
                        if not chunk:
                            break
                        f.write(chunk)

        os.replace(part_path, out_path)
        return True, ""

    except aiohttp.ClientError as e:
        try:
            if os.path.exists(part_path):
                os.remove(part_path)
        except Exception:
            pass
        return False, f"AIOHTTP {type(e).__name__}: {e}"

    except Exception as e:
        try:
            if os.path.exists(part_path):
                os.remove(part_path)
        except Exception:
            pass
        return False, f"STREAM_ERROR {type(e).__name__}: {e}"


async def download_one(context, pdf_url: str, referer: str, out_path: str) -> Tuple[bool, str]:
    part_path = out_path + ".part"

    if os.path.exists(part_path):
        try:
            os.remove(part_path)
        except Exception:
            pass

    resp = await fetch_pdf(context, pdf_url, referer)
    status = resp.status

    if status != 200:
        return False, f"HTTP {status}"

    cl = resp.headers.get("content-length") or resp.headers.get("Content-Length")
    if cl:
        try:
            size = int(cl)
            if size >= UNICORN_SIZE_BYTES:
                return await stream_download_via_aiohttp(context, pdf_url, referer, out_path, part_path)
        except Exception:
            pass

    try:
        body = await resp.body()
    except Exception as e:
        msg = str(e)
        if "Cannot create a string longer than" in msg:
            return await stream_download_via_aiohttp(context, pdf_url, referer, out_path, part_path)
        return False, f"PLAYWRIGHT_ERROR: {msg}"

    head = body[:16] if body else b""
    if not body or not bytes_look_like_pdf(head):
        try:
            if os.path.exists(part_path):
                os.remove(part_path)
        except Exception:
            pass
        return False, "SESSION_POISON"

    with open(part_path, "wb") as f:
        f.write(body)
    os.replace(part_path, out_path)
    return True, ""


async def scan_dataset_pages(
    page,
    dataset_id: int,
    base_url: str,
    out_dir: str,
    state_file: str,
    idx: Dict[str, Any],
) -> Dict[str, Any]:
    resume_page = load_resume_page(state_file)
    start_page = resume_page or max(1, int(idx["meta"].get("last_scan_page", 0)) or 1)

    log(f"[DS {dataset_id}] Scan start at page {start_page}")

    pages_no_new = 0
    page_num = start_page

    while True:
        if page_num > MAX_PAGES_HARD_CAP:
            log(f"[DS {dataset_id}] HARD CAP reached at page {page_num}. Stopping to avoid infinite loop.")
            break

        save_resume_page(state_file, page_num)
        page_url = base_url.format(page_num)

        log(f"[DS {dataset_id}] Scanning page {page_num}")
        await page.goto(page_url, wait_until="domcontentloaded")

        # Gates can randomly return; clear if they do.
        await ensure_robot_verified(page, dataset_id=dataset_id)
        await ensure_age_verified(page, dataset_id=dataset_id)

        # Let links render
        try:
            await page.wait_for_load_state("networkidle", timeout=20000)
        except PWTimeoutError:
            pass

        hrefs = await page.eval_on_selector_all(
            "a[href]",
            "els => els.map(e => e.getAttribute('href'))"
        )

        pdfs: List[Tuple[str, str]] = []
        for href in hrefs:
            if not href:
                continue
            full_url = urljoin(BASE_SITE, href)
            if is_valid_epstein_pdf_url(full_url):
                filename = os.path.basename(urlparse(full_url).path)
                if filename:
                    pdfs.append((filename, full_url))

        log(f"[DS {dataset_id}] Found {len(pdfs)} PDFs on page {page_num}")

        new_this_page = 0
        for filename, full_url in pdfs:
            if extract_file_num(filename) is None:
                continue
            if upsert_index_entry(idx, filename, full_url, page_num):
                new_this_page += 1

        if new_this_page == 0:
            pages_no_new += 1
            log(f"[DS {dataset_id}] No NEW PDFs on page {page_num} (streak={pages_no_new}/{MAX_PAGES_WITH_NO_NEW_PDFS})")
        else:
            pages_no_new = 0
            log(f"[DS {dataset_id}] NEW PDFs discovered on page {page_num}: {new_this_page}")

        idx["meta"]["last_scan_at"] = datetime.now().isoformat(timespec="seconds")
        idx["meta"]["last_scan_page"] = page_num

        safe_json_save(index_path_for_dataset(out_dir, DATASETS[dataset_id]["index_file"]), idx)

        if pages_no_new >= MAX_PAGES_WITH_NO_NEW_PDFS:
            log(f"[DS {dataset_id}] Stopping scan: no new PDFs for {MAX_PAGES_WITH_NO_NEW_PDFS} consecutive pages.")
            break

        page_num += 1
        await asyncio.sleep(SLEEP_BETWEEN_PAGES)

    return idx


async def download_missing_from_index(
    browser,
    context,
    dataset_id: int,
    out_dir: str,
    idx: Dict[str, Any],
    base_url: str,
) -> Tuple[int, Any]:
    files = idx["files"]
    completed = 0

    def sort_key(item):
        fname, entry = item
        n = extract_file_num(fname)
        return (n if n is not None else 10**18, fname)

    for filename, entry in sorted(files.items(), key=sort_key):
        if extract_file_num(filename) is None:
            continue

        out_path = os.path.join(out_dir, filename)

        if os.path.exists(out_path) and not entry.get("downloaded"):
            entry["downloaded"] = True
            entry["downloaded_at"] = datetime.now().isoformat(timespec="seconds")
            try:
                entry["bytes"] = os.path.getsize(out_path)
            except Exception:
                pass
            safe_json_save(index_path_for_dataset(out_dir, DATASETS[dataset_id]["index_file"]), idx)
            continue

        if not needs_download(out_path, entry):
            continue

        if entry.get("attempts", 0) >= MAX_DOWNLOAD_RETRIES:
            continue

        pdf_url = entry["url"]
        page_num = entry.get("page", "?")
        referer = DATASETS[dataset_id]["base_url"].format(page_num if isinstance(page_num, int) and page_num >= 1 else 1)

        log(f"[DS {dataset_id}] DOWNLOAD {filename}")

        ok, err = await download_one(context, pdf_url, referer, out_path)

        if ok:
            entry["attempts"] = int(entry.get("attempts", 0)) + 1
            entry["downloaded"] = True
            entry["downloaded_at"] = datetime.now().isoformat(timespec="seconds")
            entry["last_error"] = None
            try:
                entry["bytes"] = os.path.getsize(out_path)
            except Exception:
                pass

            completed += 1
            log(f"[DS {dataset_id}] DONE ({completed}) {filename}")

            safe_json_save(index_path_for_dataset(out_dir, DATASETS[dataset_id]["index_file"]), idx)
            await asyncio.sleep(SLEEP_BETWEEN_DOWNLOADS)
            continue

        if err == "SESSION_POISON":
            entry["last_error"] = "SESSION_POISON (non-PDF response)"
            log(f"[DS {dataset_id}] SESSION POISON DETECTED while downloading {filename}")
            safe_json_save(index_path_for_dataset(out_dir, DATASETS[dataset_id]["index_file"]), idx)

            loud_session_poison_alert(dataset_id, filename)
            log(f"[DS {dataset_id}] Auto-refreshing session context (hands-free)...")

            try:
                await context.close()
            except Exception:
                pass

            context, _auth_page = await create_fresh_context(browser, base_url.format(1), dataset_id=dataset_id)

            log(f"[DS {dataset_id}] Session refreshed. Retrying {filename}...")
            await asyncio.sleep(0.1)
            continue

        entry["attempts"] = int(entry.get("attempts", 0)) + 1
        entry["last_error"] = err
        log(f"[DS {dataset_id}] ERROR {err} for {filename}")
        safe_json_save(index_path_for_dataset(out_dir, DATASETS[dataset_id]["index_file"]), idx)
        await asyncio.sleep(SLEEP_BETWEEN_DOWNLOADS)

    return completed, context


async def process_dataset(browser, dataset_id: int, cfg: Dict[str, Any], mode: str) -> None:
    base_url = cfg["base_url"]
    out_dir = cfg["out_dir"]
    state_file = cfg["state_file"]
    index_file = cfg["index_file"]

    os.makedirs(out_dir, exist_ok=True)

    idx_path = index_path_for_dataset(out_dir, index_file)
    idx = safe_json_load(idx_path)
    idx = init_index_structure(idx, dataset_id)

    log(f"=== DATASET {dataset_id} START (mode={mode}) ===")
    log(f"Output dir: {out_dir}")
    log(f"Index file: {idx_path}")

    context = None
    auth_page = None

    try:
        context, auth_page = await create_fresh_context(browser, base_url.format(1), dataset_id=dataset_id)

        # If auth_page was closed by config, we can still scan by opening a new page.
        scan_page = auth_page
        if mode in {"scan", "sync"} and scan_page is None:
            scan_page = await context.new_page()

        if mode in {"scan", "sync"}:
            idx = await scan_dataset_pages(scan_page, dataset_id, base_url, out_dir, state_file, idx)

        if mode in {"download", "sync"}:
            completed, context = await download_missing_from_index(browser, context, dataset_id, out_dir, idx, base_url)
            log(f"[DS {dataset_id}] Download pass complete ΓÇö {completed} new PDFs")

        safe_json_save(idx_path, idx)

    except Exception as e:
        log(f"[DS {dataset_id}] FATAL ERROR: {repr(e)}")
        try:
            safe_json_save(idx_path, idx)
        except Exception:
            pass
        raise
    finally:
        if auth_page is not None:
            try:
                await auth_page.close()
            except Exception:
                pass
        if context is not None:
            try:
                await context.close()
            except Exception:
                pass

    log(f"=== DATASET {dataset_id} COMPLETE ===")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Epstein DOJ Dataset PDF downloader with scan index + resume."
    )
    p.add_argument(
        "--datasets",
        type=str,
        default="",
        help="Comma list or ranges (e.g. '1,3,5' or '1-11'). If omitted, interactive prompt is used."
    )
    p.add_argument(
        "--mode",
        type=str,
        default="",
        choices=["scan", "download", "sync", ""],
        help="scan=update index only, download=download from index only, sync=scan+download. If omitted, interactive prompt is used."
    )
    p.add_argument(
        "--headless",
        action="store_true",
        help="Run browser headless (not recommended if DOJ presents robot checks)."
    )
    return p.parse_args()


def parse_datasets_string(raw: str) -> List[int]:
    raw = raw.strip()
    if not raw:
        return []

    selected = set()
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            try:
                a, b = map(int, part.split("-", 1))
                if a > b:
                    a, b = b, a
                for n in range(a, b + 1):
                    if n in DATASETS:
                        selected.add(n)
            except ValueError:
                pass
        else:
            try:
                n = int(part)
                if n in DATASETS:
                    selected.add(n)
            except ValueError:
                pass

    return sorted(selected)


async def main():
    args = parse_args()

    chosen = parse_datasets_string(args.datasets)
    if not chosen:
        chosen = ask_datasets_interactive()

    mode = args.mode if args.mode in {"scan", "download", "sync"} else ask_mode_interactive()

    log(f"Selected datasets: {chosen}")
    log(f"Mode: {mode}")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=args.headless, slow_mo=25)

        for dataset_id in chosen:
            await process_dataset(browser, dataset_id, DATASETS[dataset_id], mode)

        await browser.close()

    log("ALL DATASETS COMPLETE")


if __name__ == "__main__":
    asyncio.run(main())
