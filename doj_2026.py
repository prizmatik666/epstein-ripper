import os
import re
import json
import time
import hashlib
import argparse
import asyncio
from urllib.parse import urljoin, urlparse
from datetime import datetime
from typing import Dict, Any, List, Tuple, Optional

from playwright.async_api import async_playwright
# UPDATED VERSION: 2/6/2026                             #
#-------------------------------------------------------#
# added a ui for dataset # selection.                   #
# added log/history support for memory persistence      #
# across sessions making re-run's less painful.         #
# I didn't realize i was only pulling up to page 220    #
# so now it goes until a dataset runs out of pages.     #
#-------------------------------------------------------#
# ================= CONFIG =================

BASE_SITE = "https://www.justice.gov"

# DOJ currently has datasets 1ΓÇô11 (adjust later if they add more)
DATASET_RANGE = range(1, 12)

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
SLEEP_BETWEEN_PAGES = 1.5

# Stop conditions
MAX_PAGES_WITH_NO_NEW_PDFS = 6     # stop after N pages in a row yield no NEW pdfs
MAX_PAGES_HARD_CAP = 200000        # safety valve to avoid infinite loops

# Retry behavior
MAX_DOWNLOAD_RETRIES = 3

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
        # corrupted index -> rename and start fresh (public-friendly behavior)
        try:
            bad = path + ".corrupt"
            os.replace(path, bad)
            log(f"WARNING: Index file corrupted, moved to {bad} and starting fresh.")
        except Exception:
            log("WARNING: Index file corrupted and could not be moved. Starting fresh.")
        return {}


def safe_json_save(path: str, data: Dict[str, Any]) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, sort_keys=True)
    os.replace(tmp, path)


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


async def create_fresh_context(browser, first_page_url: str):
    context = await browser.new_context()
    page = await context.new_page()

    log("NEW CONTEXT ΓÇö opening dataset page for DOJ auth")
    await page.goto(first_page_url, wait_until="load")

    print("\n=== AUTH REQUIRED ===")
    print("If prompted, complete DOJ robot check.")
    print("Wait until dataset file list is visible.")
    input("Press ENTER here after the list appears...\n")

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
    # Ensure keys exist
    idx.setdefault("meta", {})
    idx.setdefault("files", {})
    idx["meta"].setdefault("dataset", dataset_id)
    idx["meta"].setdefault("version", 2)
    idx["meta"].setdefault("created_at", datetime.now().isoformat(timespec="seconds"))
    idx["meta"].setdefault("last_scan_at", None)
    idx["meta"].setdefault("last_scan_page", 0)
    return idx


def upsert_index_entry(idx: Dict[str, Any], filename: str, url: str, page_num: int) -> bool:
    """
    Returns True if this was a NEW file never seen before.
    """
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

    # Update existing
    files[filename]["url"] = url
    files[filename]["last_seen"] = now
    files[filename]["page"] = page_num
    return False


def needs_download(out_path: str, entry: Dict[str, Any]) -> bool:
    # If index says downloaded, still verify file exists (public-friendly robustness)
    if entry.get("downloaded") and os.path.exists(out_path):
        return False

    # If file exists but index doesn't know, treat as downloaded and optionally hash later
    if os.path.exists(out_path) and not entry.get("downloaded"):
        return False

    return True


async def download_one(context, pdf_url: str, referer: str, out_path: str) -> Tuple[bool, str]:
    """
    Atomic download to out_path + '.part', then rename.
    Returns (ok, error_message)
    """
    part_path = out_path + ".part"

    # Ensure old partial doesn't confuse users
    if os.path.exists(part_path):
        try:
            os.remove(part_path)
        except Exception:
            pass

    resp = await fetch_pdf(context, pdf_url, referer)

    if resp.status == 200:
        body = await resp.body()
        with open(part_path, "wb") as f:
            f.write(body)
        os.replace(part_path, out_path)
        return True, ""
    return False, f"HTTP {resp.status}"


async def scan_dataset_pages(page, dataset_id: int, base_url: str, out_dir: str, state_file: str, idx: Dict[str, Any]) -> Dict[str, Any]:
    """
    Scans pages until it stops finding NEW PDFs for MAX_PAGES_WITH_NO_NEW_PDFS pages in a row.
    Updates idx in-place and persists periodically.
    """
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
        await page.goto(page_url, wait_until="networkidle")

        hrefs = await page.eval_on_selector_all(
            "a[href]",
            "els => els.map(e => e.getAttribute('href'))"
        )

        # Collect PDFs on this page
        pdfs: List[Tuple[str, str]] = []  # (filename, full_url)
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
            # Only index EFTA PDFs (keeps the tool focused and predictable)
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

        # Update meta + persist
        idx["meta"]["last_scan_at"] = datetime.now().isoformat(timespec="seconds")
        idx["meta"]["last_scan_page"] = page_num

        # Save index every page (safer for public users)
        safe_json_save(index_path_for_dataset(out_dir, DATASETS[dataset_id]["index_file"]), idx)

        if pages_no_new >= MAX_PAGES_WITH_NO_NEW_PDFS:
            log(f"[DS {dataset_id}] Stopping scan: no new PDFs for {MAX_PAGES_WITH_NO_NEW_PDFS} consecutive pages.")
            break

        page_num += 1
        await asyncio.sleep(SLEEP_BETWEEN_PAGES)

    return idx


async def download_missing_from_index(context, dataset_id: int, out_dir: str, idx: Dict[str, Any]) -> int:
    """
    Downloads any indexed PDFs that aren't downloaded yet.
    Returns count of newly downloaded files.
    """
    files = idx["files"]
    completed = 0

    # Sort by file number for a stable, human-friendly order
    def sort_key(item):
        fname, entry = item
        n = extract_file_num(fname)
        return (n if n is not None else 10**18, fname)

    for filename, entry in sorted(files.items(), key=sort_key):
        # Only handle EFTA*.pdf
        if extract_file_num(filename) is None:
            continue

        out_path = os.path.join(out_dir, filename)

        # If file already exists, mark downloaded (public-friendly reconciliation)
        if os.path.exists(out_path) and not entry.get("downloaded"):
            entry["downloaded"] = True
            entry["downloaded_at"] = datetime.now().isoformat(timespec="seconds")
            try:
                entry["bytes"] = os.path.getsize(out_path)
            except Exception:
                pass
            # hash is optional (can be slow), but you can enable later
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
        entry["attempts"] = int(entry.get("attempts", 0)) + 1

        ok, err = await download_one(context, pdf_url, referer, out_path)

        if ok:
            entry["downloaded"] = True
            entry["downloaded_at"] = datetime.now().isoformat(timespec="seconds")
            entry["last_error"] = None
            try:
                entry["bytes"] = os.path.getsize(out_path)
            except Exception:
                pass

            completed += 1
            log(f"[DS {dataset_id}] DONE ({completed}) {filename}")

            # Persist after each success (crash-safe)
            safe_json_save(index_path_for_dataset(out_dir, DATASETS[dataset_id]["index_file"]), idx)
            await asyncio.sleep(SLEEP_BETWEEN_DOWNLOADS)
        else:
            entry["last_error"] = err
            log(f"[DS {dataset_id}] ERROR {err} for {filename}")
            safe_json_save(index_path_for_dataset(out_dir, DATASETS[dataset_id]["index_file"]), idx)
            await asyncio.sleep(SLEEP_BETWEEN_DOWNLOADS)

    return completed


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

    # Always open auth context if we may scan OR download
    context = None
    page = None

    try:
        context, page = await create_fresh_context(browser, base_url.format(1))

        if mode in {"scan", "sync"}:
            idx = await scan_dataset_pages(page, dataset_id, base_url, out_dir, state_file, idx)

        if mode in {"download", "sync"}:
            # Download uses context.request, so we don't need the visual page except for auth gating
            completed = await download_missing_from_index(context, dataset_id, out_dir, idx)
            log(f"[DS {dataset_id}] Download pass complete ΓÇö {completed} new PDFs")

        # Save final index
        safe_json_save(idx_path, idx)

    except Exception as e:
        log(f"[DS {dataset_id}] FATAL ERROR: {repr(e)}")
        # still persist index so rerun can continue cleanly
        try:
            safe_json_save(idx_path, idx)
        except Exception:
            pass
        raise
    finally:
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

