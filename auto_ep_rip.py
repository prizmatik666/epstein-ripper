#!/usr/bin/env python3
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
#   [ AUTOMATIC ]                       ███    ███ version 3.1
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
# [ 3/03/2026 ] UPDATED VERSION                         #
#_______________________________________________________#
# Hands-free upgrades:
# - Auto-click abuse-deterrent "I am not a robot" button (reauth gate)
# - Auto-click age gate YES (#age-button-yes)
# - No more "Press ENTER..." pauses for session refresh
# - Waits until dataset list is visible, then resumes automatically
# - Adds configurable sleeps between auth stages (stability)
#
# Patch vNext:
# - Prevent infinite loops on bad PDFs / poison: per-file poison cap + immediate skip for clearly bad payloads
# - bad_files.log audit trail for skipped/bad-source files
# - Retryable network error handling (ETIMEDOUT/ECONNRESET/socket hang up/etc) with backoff
#
# Patch (session polish):
# - Bad-file messaging: "BAD_SERVER_FILE (PDF endpoint returned non-PDF bytes)"
# - Ctrl+C / shutdown session summary stats (downloaded, bad/skips, net errors, etc.)
# - Warmup REMOVED: replaced with clean, confidence-forward initialization + settle delay
#
# Patch (poison retry fix):
# - After session refresh/re-auth, ACTUALLY retry the same file before moving on
#   (inner per-file loop; refresh triggers a retry of the current filename)
#=======================================================#
#=======================================================#
# UPDATED VERSION: 3/2/2026 [auto_ep_rip.py]            #
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
#!/usr/bin/env python3
# Epstein DOJ Dataset Tools
#
# Author: Prizm (Prizmatik Underground)
# Repository:
# https://github.com/prizmatik666/epstein-ripper
#
# Support Development:
# PayPal: https://www.paypal.com/ncp/payment/VVDAXZGKPQZKW
# Email: prizmatikug@gmail.com
#==========================================================#
import os
import re
import json
import time
import hashlib
import argparse
import asyncio
import sys
import random
from urllib.parse import urljoin, urlparse
from datetime import datetime
from typing import Dict, Any, List, Tuple, Optional

import aiohttp
from playwright.async_api import async_playwright
from playwright.async_api import TimeoutError as PWTimeoutError

# ================= CONFIG =================

BASE_SITE = "https://www.justice.gov"

# DOJ currently has datasets 1-12 (adjust later if they add more)
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
BAD_FILES_LOG = "bad_files.log"

# Throttling
SLEEP_BETWEEN_DOWNLOADS = 0.05
SLEEP_BETWEEN_PAGES = 0.5

# Stop conditions
MAX_PAGES_WITH_NO_NEW_PDFS = 300
MAX_PAGES_HARD_CAP = 200000

# Retry behavior (for real download failures; poison has its own counters)
MAX_DOWNLOAD_RETRIES = 3

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

# --- Auth stability sleeps ---
AUTH_SLEEP_AFTER_GOTO = 1.5
AUTH_SLEEP_AFTER_ROBOT_CLICK = 1.0
AUTH_SLEEP_AFTER_AGE_CLICK = 0.6
AUTH_SLEEP_AFTER_LIST_VISIBLE = 0.8

# --- Initialization settle delay (replaces warmup) ---
# This gives the session/cookies a moment to fully lock in before downloads begin.
AUTH_SESSION_SETTLE_SECONDS = 1.0

# Keep the auth page open for a short time after list is visible (helps some flaky sessions).
KEEP_AUTH_PAGE_OPEN_SECONDS = 5.0  # set 0 to disable
# If True, we will close the auth page automatically after settle + keep-open delay.
CLOSE_AUTH_PAGE_AFTER_AUTH = True

# --- Poison handling / audit ---
POISON_HITS_BEFORE_SKIP = 3          # N poison hits => skip + bad_files.log
POISON_REFRESHES_BEFORE_SKIP = 2     # refresh context at most this many times for same file
BAD_PDF_IMMEDIATE_SKIP = True        # if clearly bad payload (non-PDF, non-HTML), skip immediately

# --- Backoff for retryable network failures ---
RETRY_BACKOFF_BASE = 0.8
RETRY_BACKOFF_CAP = 15.0
RETRY_BACKOFF_JITTER = 0.35

# =========================================

SESSION = {
    "start_ts": datetime.now().isoformat(timespec="seconds"),
    "downloaded_ok": 0,
    "marked_downloaded_existing": 0,
    "skipped_bad_server_file": 0,
    "skipped_poison_hit_limit": 0,
    "skipped_poison_refresh_limit": 0,
    "skipped_max_retries": 0,
    "retryable_net_errors": 0,
    "http_errors": 0,
    "other_errors": 0,
    "datasets": {},  # ds -> dict
}


def _ds_stats(ds: int) -> Dict[str, int]:
    d = SESSION["datasets"].setdefault(ds, {
        "downloaded_ok": 0,
        "marked_downloaded_existing": 0,
        "skipped_bad_server_file": 0,
        "skipped_poison_hit_limit": 0,
        "skipped_poison_refresh_limit": 0,
        "skipped_max_retries": 0,
        "retryable_net_errors": 0,
        "http_errors": 0,
        "other_errors": 0,
    })
    return d


def _append_to_logfile(lines: List[str]) -> None:
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            for ln in lines:
                f.write(ln + "\n")
    except Exception:
        pass


def print_session_summary() -> None:
    end_ts = datetime.now().isoformat(timespec="seconds")
    lines = []
    lines.append("=" * 78)
    lines.append(f"SESSION SUMMARY  start={SESSION['start_ts']}  end={end_ts}")
    lines.append("-" * 78)
    lines.append(f"Downloaded OK:                 {SESSION['downloaded_ok']}")
    lines.append(f"Marked downloaded (pre-exist): {SESSION['marked_downloaded_existing']}")
    lines.append(f"Skipped (bad server file):     {SESSION['skipped_bad_server_file']}")
    lines.append(f"Skipped (poison hit limit):    {SESSION['skipped_poison_hit_limit']}")
    lines.append(f"Skipped (poison refresh limit):{SESSION['skipped_poison_refresh_limit']}")
    lines.append(f"Skipped (max retries):         {SESSION['skipped_max_retries']}")
    lines.append(f"Retryable net errors:          {SESSION['retryable_net_errors']}")
    lines.append(f"HTTP errors:                   {SESSION['http_errors']}")
    lines.append(f"Other errors:                  {SESSION['other_errors']}")

    if SESSION["datasets"]:
        lines.append("")
        lines.append("Per-dataset:")
        for ds in sorted(SESSION["datasets"].keys()):
            s = SESSION["datasets"][ds]
            lines.append(
                f"  DS{ds}: ok={s['downloaded_ok']} exist={s['marked_downloaded_existing']} "
                f"bad={s['skipped_bad_server_file']} poison={s['skipped_poison_hit_limit']} "
                f"refresh_limit={s['skipped_poison_refresh_limit']} max={s['skipped_max_retries']} "
                f"net={s['retryable_net_errors']} http={s['http_errors']} other={s['other_errors']}"
            )

    lines.append("=" * 78)

    # Print to terminal
    print("\n" + "\n".join(lines) + "\n")

    # Also append to download.log (without timestamps; it's a summary block)
    _append_to_logfile([""] + lines + [""])


def log(msg: str) -> None:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def log_bad_file(dataset_id: int, filename: str, url: str, referer: str, reason: str, extra: str = "") -> None:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = (
        f"[{ts}] DS={dataset_id} FILE={filename} REASON={reason} "
        f"URL={url} REFERER={referer}"
    )
    if extra:
        line += f" EXTRA={extra}"
    print(line)
    with open(BAD_FILES_LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def is_retryable_playwright_error(msg: str) -> bool:
    m = (msg or "").lower()
    retry_signals = [
        "etimedout",
        "econnreset",
        "socket hang up",
        "eai_again",
        "net::err_connection_reset",
        "net::err_connection_closed",
        "net::err_timed_out",
        "timed out",
        "read etimedout",
        "connection terminated",
        "connection closed",
        "temporary failure in name resolution",
        "name resolution",
    ]
    return any(s in m for s in retry_signals)


def backoff_sleep_seconds(attempt_num: int) -> float:
    base = min(RETRY_BACKOFF_CAP, RETRY_BACKOFF_BASE * (2 ** max(0, attempt_num - 1)))
    jitter = base * random.uniform(0.0, RETRY_BACKOFF_JITTER)
    return base + jitter


def classify_non_pdf_bytes(head16: bytes) -> str:
    """
    Returns:
      'HTML_GATE'   -> looks like HTML / gate page
      'BAD_PAYLOAD' -> not html, not PDF magic (likely corrupt or wrong content)
    """
    h = (head16 or b"").lstrip()
    if h.startswith(b"\xef\xbb\xbf"):
        h = h[3:].lstrip()
    hl = h.lower()
    if (
        hl.startswith(b"<!doctype") or
        hl.startswith(b"<html") or
        hl.startswith(b"<head") or
        hl.startswith(b"<script") or
        hl.startswith(b"<!--") or
        hl.startswith(b"<meta") or
        hl.startswith(b"<title") or
        hl.startswith(b"<?xml")
    ):
        return "HTML_GATE"
    return "BAD_PAYLOAD"


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
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)

    tmp = path + ".tmp"

    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, sort_keys=True)
        f.flush()
        try:
            os.fsync(f.fileno())
        except Exception:
            pass

    for attempt in range(3):
        try:
            os.replace(tmp, path)
            return
        except FileNotFoundError:
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
    try:
        btn = page.locator(ROBOT_BTN).first
        if await btn.is_visible(timeout=250):
            log(f"[DS {dataset_id}] [auth] Robot gate detected -> clicking 'I am not a robot'")
            await btn.click(timeout=8000)
            await asyncio.sleep(AUTH_SLEEP_AFTER_ROBOT_CLICK)
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
    log(f"[DS {dataset_id}] [auth] Validating access and loading dataset list...")
    deadline = time.time() + timeout_s

    while True:
        await ensure_robot_verified(page, dataset_id=dataset_id)
        await ensure_age_verified(page, dataset_id=dataset_id)

        try:
            count = await page.locator(DATASET_LIST_PDF_LINKS).count()
            if count and count > 0:
                log(f"[DS {dataset_id}] [auth] Dataset list ready (pdf links found: {count})")
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


async def create_fresh_context(browser, first_page_url: str, dataset_id: int):
    context = await browser.new_context()
    page = await context.new_page()

    log(f"[DS {dataset_id}] NEW CONTEXT — starting DOJ session...")
    await page.goto(first_page_url, wait_until="domcontentloaded")
    await asyncio.sleep(AUTH_SLEEP_AFTER_GOTO)

    await ensure_robot_verified(page, dataset_id=dataset_id)
    await ensure_age_verified(page, dataset_id=dataset_id)
    await wait_for_dataset_list(page, dataset_id=dataset_id, timeout_s=AUTH_WAIT_SECONDS)

    if AUTH_SESSION_SETTLE_SECONDS and AUTH_SESSION_SETTLE_SECONDS > 0:
        log(f"[DS {dataset_id}] [auth] Session initialized — settling ({AUTH_SESSION_SETTLE_SECONDS:.1f}s)")
        await asyncio.sleep(AUTH_SESSION_SETTLE_SECONDS)

    if KEEP_AUTH_PAGE_OPEN_SECONDS and KEEP_AUTH_PAGE_OPEN_SECONDS > 0:
        log(f"[DS {dataset_id}] [auth] Holding auth window open ({KEEP_AUTH_PAGE_OPEN_SECONDS:.1f}s)")
        await asyncio.sleep(KEEP_AUTH_PAGE_OPEN_SECONDS)

    if CLOSE_AUTH_PAGE_AFTER_AUTH:
        try:
            await page.close()
            page = None
            log(f"[DS {dataset_id}] [auth] Session ready — proceeding to work queue")
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
                "version": 3,
            },
            "files": {}
        }
    idx.setdefault("meta", {})
    idx.setdefault("files", {})
    idx["meta"].setdefault("dataset", dataset_id)
    idx["meta"].setdefault("version", 3)
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

            "poison_hits": 0,
            "poison_refreshes": 0,
            "skipped": False,
            "skip_reason": None,
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
    if not b:
        return False
    return b.lstrip().startswith(b"%PDF-")


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
                    kind = classify_non_pdf_bytes(first)
                    return False, ("SESSION_POISON_HTML" if kind == "HTML_GATE" else "SESSION_POISON_BAD")

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

    try:
        resp = await fetch_pdf(context, pdf_url, referer)
    except Exception as e:
        msg = str(e)
        if is_retryable_playwright_error(msg):
            return False, f"RETRYABLE_NET: {msg}"
        return False, f"PLAYWRIGHT_ERROR: {msg}"

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
        if is_retryable_playwright_error(msg):
            return False, f"RETRYABLE_NET: {msg}"
        return False, f"PLAYWRIGHT_ERROR: {msg}"

    head = body[:16] if body else b""
    if not body or not bytes_look_like_pdf(head):
        try:
            if os.path.exists(part_path):
                os.remove(part_path)
        except Exception:
            pass
        kind = classify_non_pdf_bytes(head)
        return False, ("SESSION_POISON_HTML" if kind == "HTML_GATE" else "SESSION_POISON_BAD")

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

        await ensure_robot_verified(page, dataset_id=dataset_id)
        await ensure_age_verified(page, dataset_id=dataset_id)

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

        if entry.get("skipped"):
            continue

        out_path = os.path.join(out_dir, filename)

        if os.path.exists(out_path) and not entry.get("downloaded"):
            entry["downloaded"] = True
            entry["downloaded_at"] = datetime.now().isoformat(timespec="seconds")
            try:
                entry["bytes"] = os.path.getsize(out_path)
            except Exception:
                pass
            SESSION["marked_downloaded_existing"] += 1
            _ds_stats(dataset_id)["marked_downloaded_existing"] += 1
            safe_json_save(index_path_for_dataset(out_dir, DATASETS[dataset_id]["index_file"]), idx)
            continue

        if not needs_download(out_path, entry):
            continue

        # If attempts already exhausted, skip immediately (and audit)
        if entry.get("attempts", 0) >= MAX_DOWNLOAD_RETRIES:
            entry["skipped"] = True
            entry["skip_reason"] = "MAX_RETRIES_EXHAUSTED"
            entry["last_error"] = entry["skip_reason"]
            SESSION["skipped_max_retries"] += 1
            _ds_stats(dataset_id)["skipped_max_retries"] += 1

            pdf_url = entry.get("url", "")
            page_num = entry.get("page", "?")
            referer = DATASETS[dataset_id]["base_url"].format(page_num if isinstance(page_num, int) and page_num >= 1 else 1)

            log(f"[DS {dataset_id}] MAX RETRIES EXHAUSTED -> SKIPPING {filename}")
            log_bad_file(dataset_id, filename, pdf_url, referer, entry["skip_reason"], extra=f"attempts={entry.get('attempts', 0)}")
            safe_json_save(index_path_for_dataset(out_dir, DATASETS[dataset_id]["index_file"]), idx)
            continue

        pdf_url = entry["url"]
        page_num = entry.get("page", "?")
        referer = DATASETS[dataset_id]["base_url"].format(page_num if isinstance(page_num, int) and page_num >= 1 else 1)

        # ---------------------------------------------------------------------
        # FIX: per-file loop so a session refresh actually retries THIS filename
        # ---------------------------------------------------------------------
        while True:
            # Respect max retries during the loop too
            if entry.get("attempts", 0) >= MAX_DOWNLOAD_RETRIES:
                entry["skipped"] = True
                entry["skip_reason"] = "MAX_RETRIES_EXHAUSTED"
                entry["last_error"] = entry["skip_reason"]
                SESSION["skipped_max_retries"] += 1
                _ds_stats(dataset_id)["skipped_max_retries"] += 1

                log(f"[DS {dataset_id}] MAX RETRIES EXHAUSTED -> SKIPPING {filename}")
                log_bad_file(dataset_id, filename, pdf_url, referer, entry["skip_reason"], extra=f"attempts={entry.get('attempts', 0)}")
                safe_json_save(index_path_for_dataset(out_dir, DATASETS[dataset_id]["index_file"]), idx)
                break  # move to next file

            log(f"[DS {dataset_id}] DOWNLOAD {filename}")
            ok, err = await download_one(context, pdf_url, referer, out_path)

            if ok:
                entry["attempts"] = int(entry.get("attempts", 0)) + 1
                entry["downloaded"] = True
                entry["downloaded_at"] = datetime.now().isoformat(timespec="seconds")
                entry["last_error"] = None
                entry["poison_hits"] = 0
                entry["poison_refreshes"] = 0

                try:
                    entry["bytes"] = os.path.getsize(out_path)
                except Exception:
                    pass

                completed += 1
                SESSION["downloaded_ok"] += 1
                _ds_stats(dataset_id)["downloaded_ok"] += 1

                log(f"[DS {dataset_id}] DONE ({completed}) {filename}")
                safe_json_save(index_path_for_dataset(out_dir, DATASETS[dataset_id]["index_file"]), idx)
                await asyncio.sleep(SLEEP_BETWEEN_DOWNLOADS)
                break  # move to next file

            # --- Poison handling ---
            if err in ("SESSION_POISON_HTML", "SESSION_POISON_BAD"):
                poison_hits = int(entry.get("poison_hits", 0)) + 1
                entry["poison_hits"] = poison_hits

                if err == "SESSION_POISON_BAD" and BAD_PDF_IMMEDIATE_SKIP:
                    entry["skipped"] = True
                    entry["skip_reason"] = "BAD_SERVER_FILE (PDF endpoint returned non-PDF bytes)"
                    entry["last_error"] = entry["skip_reason"]

                    SESSION["skipped_bad_server_file"] += 1
                    _ds_stats(dataset_id)["skipped_bad_server_file"] += 1

                    log(f"[DS {dataset_id}] BAD SERVER FILE -> SKIPPING {filename}")
                    log_bad_file(dataset_id, filename, pdf_url, referer, entry["skip_reason"], extra=f"poison_hits={poison_hits}")
                    safe_json_save(index_path_for_dataset(out_dir, DATASETS[dataset_id]["index_file"]), idx)
                    await asyncio.sleep(SLEEP_BETWEEN_DOWNLOADS)
                    break  # move to next file

                # HTML gate / poison
                entry["last_error"] = f"{err} (non-PDF response)"
                log(f"[DS {dataset_id}] SESSION POISON ({err}) while downloading {filename} (hit {poison_hits}/{POISON_HITS_BEFORE_SKIP})")
                safe_json_save(index_path_for_dataset(out_dir, DATASETS[dataset_id]["index_file"]), idx)

                if poison_hits >= POISON_HITS_BEFORE_SKIP:
                    entry["skipped"] = True
                    entry["skip_reason"] = f"REPEATED_SESSION_POISON ({err})"
                    entry["last_error"] = entry["skip_reason"]

                    SESSION["skipped_poison_hit_limit"] += 1
                    _ds_stats(dataset_id)["skipped_poison_hit_limit"] += 1

                    log(f"[DS {dataset_id}] POISON HIT LIMIT -> SKIPPING {filename}")
                    log_bad_file(dataset_id, filename, pdf_url, referer, entry["skip_reason"], extra=f"poison_hits={poison_hits}")
                    safe_json_save(index_path_for_dataset(out_dir, DATASETS[dataset_id]["index_file"]), idx)
                    await asyncio.sleep(SLEEP_BETWEEN_DOWNLOADS)
                    break  # move to next file

                refreshes = int(entry.get("poison_refreshes", 0))
                if refreshes >= POISON_REFRESHES_BEFORE_SKIP:
                    entry["skipped"] = True
                    entry["skip_reason"] = f"POISON_REFRESH_LIMIT ({err})"
                    entry["last_error"] = entry["skip_reason"]

                    SESSION["skipped_poison_refresh_limit"] += 1
                    _ds_stats(dataset_id)["skipped_poison_refresh_limit"] += 1

                    log(f"[DS {dataset_id}] POISON REFRESH LIMIT -> SKIPPING {filename}")
                    log_bad_file(dataset_id, filename, pdf_url, referer, entry["skip_reason"], extra=f"poison_hits={poison_hits},refreshes={refreshes}")
                    safe_json_save(index_path_for_dataset(out_dir, DATASETS[dataset_id]["index_file"]), idx)
                    await asyncio.sleep(SLEEP_BETWEEN_DOWNLOADS)
                    break  # move to next file

                # Refresh session and then retry SAME file (inner-loop continue)
                loud_session_poison_alert(dataset_id, filename)
                log(f"[DS {dataset_id}] Auto-refreshing session context (hands-free)... (refresh {refreshes+1}/{POISON_REFRESHES_BEFORE_SKIP})")
                entry["poison_refreshes"] = refreshes + 1
                safe_json_save(index_path_for_dataset(out_dir, DATASETS[dataset_id]["index_file"]), idx)

                try:
                    await context.close()
                except Exception:
                    pass

                context, _auth_page = await create_fresh_context(browser, base_url.format(1), dataset_id=dataset_id)
                log(f"[DS {dataset_id}] Session refreshed. Retrying {filename}...")
                await asyncio.sleep(0.2)
                continue  # <-- ACTUALLY retries the same filename now

            # --- Normal error path ---
            entry["attempts"] = int(entry.get("attempts", 0)) + 1
            entry["last_error"] = err

            log(f"[DS {dataset_id}] ERROR {err} for {filename}")
            safe_json_save(index_path_for_dataset(out_dir, DATASETS[dataset_id]["index_file"]), idx)

            if isinstance(err, str) and err.startswith("HTTP "):
                SESSION["http_errors"] += 1
                _ds_stats(dataset_id)["http_errors"] += 1
            else:
                SESSION["other_errors"] += 1
                _ds_stats(dataset_id)["other_errors"] += 1

            is_retryable = False
            if isinstance(err, str) and (err.startswith("RETRYABLE_NET:") or err.startswith("AIOHTTP ")):
                is_retryable = True
            elif isinstance(err, str) and is_retryable_playwright_error(err):
                is_retryable = True

            if is_retryable:
                SESSION["retryable_net_errors"] += 1
                _ds_stats(dataset_id)["retryable_net_errors"] += 1
                delay = backoff_sleep_seconds(int(entry.get("attempts", 1)))
                log(f"[DS {dataset_id}] Retryable network error -> backoff {delay:.2f}s")
                await asyncio.sleep(delay)
                continue  # retry same file (until attempts exhausted)
            else:
                await asyncio.sleep(SLEEP_BETWEEN_DOWNLOADS)
                break  # non-retryable -> move to next file

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

        scan_page = auth_page
        if mode in {"scan", "sync"} and scan_page is None:
            scan_page = await context.new_page()

        if mode in {"scan", "sync"}:
            idx = await scan_dataset_pages(scan_page, dataset_id, base_url, out_dir, state_file, idx)

        if mode in {"download", "sync"}:
            completed, context = await download_missing_from_index(browser, context, dataset_id, out_dir, idx, base_url)
            log(f"[DS {dataset_id}] Download pass complete — {completed} new PDFs")

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
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log("KeyboardInterrupt received — shutting down.")
    finally:
        print_session_summary()
