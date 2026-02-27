#!/usr/bin/env python3
"""
epstein_sql_indexer.py
SQLite-backed DOJ Epstein dataset scanner/indexer with:
- DOJ-auth Playwright flow (same style as your ripper)
- Full interactive UI (dataset pick 1-12 + scan modes)
- Resume-friendly state (next_page + no_new_streak)
- Repair mode for suspect pages (0 PDFs / errors / repeat-page fingerprints)
- Two-phase runs: REWALK (no-new streak OFF) -> DISCOVERY (no-new streak ON)
"""

import os
import re
import sys
import argparse
import asyncio
import sqlite3
import hashlib
from datetime import datetime
from typing import Optional, List, Tuple, Set
from urllib.parse import urljoin, urlparse

from playwright.async_api import async_playwright

# ================= CONFIG =================

BASE_SITE = "https://www.justice.gov"

# datasets 1-12
DATASET_RANGE = range(1, 13)

DATASETS = {
    n: {
        "base_url": f"https://www.justice.gov/epstein/doj-disclosures/data-set-{n}-files?page={{}}",
        "out_dir": f"data{n}",
        "db_file": f"index_data{n}.sqlite",
    }
    for n in DATASET_RANGE
}

LOG_FILE = "indexer.log"

# throttling
SLEEP_BETWEEN_PAGES = 0.5

# stop conditions (discovery mode only)
MAX_PAGES_WITH_NO_NEW_PDFS = 300
MAX_PAGES_HARD_CAP = 200000

# suspect logic
MARK_ZERO_PDFS_AS_SUSPECT = True
MARK_REPEAT_PAGES_AS_SUSPECT = True  # page fingerprint identical to previous page

# navigation timeout
PAGE_GOTO_TIMEOUT_MS = 120000

# =========================================


def now_ts() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def log(msg: str) -> None:
    line = f"[{now_ts()}] {msg}"
    print(line)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def extract_file_num(filename: str) -> Optional[int]:
    m = re.match(r"EFTA0*(\d+)\.pdf$", filename, re.IGNORECASE)
    if not m:
        return None
    return int(m.group(1))


def is_valid_epstein_pdf_url(full_url: str) -> bool:
    u = full_url.lower()
    return ("/epstein/files/" in u) and u.endswith(".pdf")


def db_path_for_dataset(out_dir: str, db_file: str) -> str:
    return os.path.join(out_dir, db_file)


def connect_db(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA temp_store=MEMORY;")
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn


def init_db(conn: sqlite3.Connection, dataset_id: int) -> None:
    """
    Creates schema (and applies simple migrations safely).
    """
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS pages (
            dataset_id INTEGER NOT NULL,
            page_num INTEGER NOT NULL,
            scanned_at TEXT,
            status TEXT NOT NULL,              -- ok | suspect_zero | suspect_repeat | error
            pdf_found INTEGER NOT NULL DEFAULT 0,
            efta_found INTEGER NOT NULL DEFAULT 0,
            new_files INTEGER NOT NULL DEFAULT 0,
            page_hash TEXT,                    -- fingerprint of EFTA filenames for repeat detection
            error TEXT,
            PRIMARY KEY (dataset_id, page_num)
        );

        CREATE TABLE IF NOT EXISTS files (
            dataset_id INTEGER NOT NULL,
            filename TEXT NOT NULL,
            file_num INTEGER,
            url TEXT NOT NULL,
            first_seen TEXT NOT NULL,
            last_seen TEXT NOT NULL,
            last_page INTEGER,
            PRIMARY KEY (dataset_id, filename)
        );

        CREATE TABLE IF NOT EXISTS resume_state (
            dataset_id INTEGER PRIMARY KEY,
            next_page INTEGER NOT NULL DEFAULT 1,
            no_new_streak INTEGER NOT NULL DEFAULT 0,
            last_scan_at TEXT,
            last_scan_page INTEGER NOT NULL DEFAULT 0
        );

        CREATE INDEX IF NOT EXISTS idx_pages_status ON pages(dataset_id, status);
        CREATE INDEX IF NOT EXISTS idx_pages_hash ON pages(dataset_id, page_hash);
        CREATE INDEX IF NOT EXISTS idx_files_num ON files(dataset_id, file_num);
        """
    )

    # meta defaults
    def meta_set_default(k: str, v: str):
        conn.execute("INSERT OR IGNORE INTO meta(key, value) VALUES(?, ?)", (k, v))

    meta_set_default("version", "2")
    meta_set_default("created_at", datetime.now().isoformat(timespec="seconds"))
    meta_set_default("dataset_id", str(dataset_id))

    # resume row
    conn.execute(
        "INSERT OR IGNORE INTO resume_state(dataset_id, next_page, no_new_streak, last_scan_at, last_scan_page) "
        "VALUES(?, 1, 0, NULL, 0)",
        (dataset_id,),
    )
    conn.commit()


def get_resume_state(conn: sqlite3.Connection, dataset_id: int) -> Tuple[int, int, int]:
    row = conn.execute(
        "SELECT next_page, no_new_streak, last_scan_page FROM resume_state WHERE dataset_id=?",
        (dataset_id,),
    ).fetchone()
    if not row:
        return 1, 0, 0
    return int(row["next_page"]), int(row["no_new_streak"]), int(row["last_scan_page"])


def set_resume_state(conn: sqlite3.Connection, dataset_id: int, next_page: int, no_new_streak: int, last_scan_page: int) -> None:
    conn.execute(
        "UPDATE resume_state SET next_page=?, no_new_streak=?, last_scan_at=?, last_scan_page=? WHERE dataset_id=?",
        (next_page, no_new_streak, datetime.now().isoformat(timespec="seconds"), last_scan_page, dataset_id),
    )


def get_frontier_page(conn: sqlite3.Connection, dataset_id: int) -> int:
    """
    "Known frontier" = highest page we've ever recorded in pages table.
    Used to hand off from REWALK -> DISCOVERY.
    """
    row = conn.execute(
        "SELECT MAX(page_num) AS m FROM pages WHERE dataset_id=?",
        (dataset_id,),
    ).fetchone()
    m = row["m"] if row else None
    return int(m) if m is not None else 0


def get_prev_page_hash(conn: sqlite3.Connection, dataset_id: int, page_num: int) -> Optional[str]:
    """
    Return hash from page_num-1 if exists.
    """
    if page_num <= 1:
        return None
    row = conn.execute(
        "SELECT page_hash FROM pages WHERE dataset_id=? AND page_num=?",
        (dataset_id, page_num - 1),
    ).fetchone()
    if not row:
        return None
    return row["page_hash"]


def upsert_page_result(
    conn: sqlite3.Connection,
    dataset_id: int,
    page_num: int,
    status: str,
    pdf_found: int,
    efta_found: int,
    new_files: int,
    page_hash: Optional[str],
    error: Optional[str],
) -> None:
    conn.execute(
        """
        INSERT INTO pages(dataset_id, page_num, scanned_at, status, pdf_found, efta_found, new_files, page_hash, error)
        VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(dataset_id, page_num) DO UPDATE SET
            scanned_at=excluded.scanned_at,
            status=excluded.status,
            pdf_found=excluded.pdf_found,
            efta_found=excluded.efta_found,
            new_files=excluded.new_files,
            page_hash=excluded.page_hash,
            error=excluded.error
        """,
        (
            dataset_id,
            page_num,
            datetime.now().isoformat(timespec="seconds"),
            status,
            int(pdf_found),
            int(efta_found),
            int(new_files),
            page_hash,
            error,
        ),
    )


def upsert_file(
    conn: sqlite3.Connection,
    dataset_id: int,
    filename: str,
    file_num: Optional[int],
    url: str,
    page_num: int,
) -> bool:
    """
    Returns True if NEW file inserted, False if it already existed.
    """
    now = datetime.now().isoformat(timespec="seconds")
    cur = conn.execute(
        """
        INSERT OR IGNORE INTO files(dataset_id, filename, file_num, url, first_seen, last_seen, last_page)
        VALUES(?, ?, ?, ?, ?, ?, ?)
        """,
        (dataset_id, filename, file_num, url, now, now, page_num),
    )
    inserted = (cur.rowcount == 1)

    if not inserted:
        conn.execute(
            """
            UPDATE files
            SET url=?, last_seen=?, last_page=?, file_num=COALESCE(file_num, ?)
            WHERE dataset_id=? AND filename=?
            """,
            (url, now, page_num, file_num, dataset_id, filename),
        )

    return inserted


def list_suspect_pages(conn: sqlite3.Connection, dataset_id: int, limit: Optional[int] = None) -> List[int]:
    q = """
        SELECT page_num
        FROM pages
        WHERE dataset_id=? AND status IN ('suspect_zero','suspect_repeat','error')
        ORDER BY page_num
    """
    params = [dataset_id]
    if limit is not None:
        q += " LIMIT ?"
        params.append(int(limit))
    rows = conn.execute(q, params).fetchall()
    return [int(r["page_num"]) for r in rows]


def print_stats(conn: sqlite3.Connection, dataset_id: int) -> None:
    total_files = conn.execute(
        "SELECT COUNT(*) AS c FROM files WHERE dataset_id=?",
        (dataset_id,),
    ).fetchone()["c"]
    total_pages = conn.execute(
        "SELECT COUNT(*) AS c FROM pages WHERE dataset_id=?",
        (dataset_id,),
    ).fetchone()["c"]
    suspect_pages = conn.execute(
        "SELECT COUNT(*) AS c FROM pages WHERE dataset_id=? AND status IN ('suspect_zero','suspect_repeat','error')",
        (dataset_id,),
    ).fetchone()["c"]
    next_page, streak, last_scan_page = get_resume_state(conn, dataset_id)
    frontier = get_frontier_page(conn, dataset_id)

    print("\n=== DB STATS ===")
    print(f"Dataset: {dataset_id}")
    print(f"Files indexed: {total_files}")
    print(f"Pages scanned: {total_pages}")
    print(f"Suspect/error pages: {suspect_pages}")
    print(f"Frontier (max page scanned): {frontier}")
    print(f"Resume next_page: {next_page}")
    print(f"Resume no_new_streak: {streak}")
    print(f"Last scan page: {last_scan_page}")
    print("================\n")


async def create_fresh_context(browser, first_page_url: str):
    context = await browser.new_context()
    page = await context.new_page()

    log("NEW CONTEXT ΓÇö opening dataset page for DOJ auth")
    await page.goto(first_page_url, wait_until="load", timeout=PAGE_GOTO_TIMEOUT_MS)

    print("\n=== AUTH REQUIRED ===")
    print("If prompted, complete DOJ robot check.")
    print("Wait until dataset file list is visible.")
    input("Press ENTER here after the list appears...\n")

    return context, page


async def scrape_page_for_pdfs(page, page_url: str) -> Tuple[List[Tuple[str, str]], Optional[str]]:
    """
    Returns (pdfs, error). pdfs = [(filename, full_url)]
    Any exception becomes error string.
    """
    try:
        await page.goto(page_url, wait_until="networkidle", timeout=PAGE_GOTO_TIMEOUT_MS)

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

        return pdfs, None

    except Exception as e:
        return [], repr(e)


def fingerprint_filenames(names: List[str]) -> str:
    """
    Stable page fingerprint of filenames (already filtered).
    """
    s = "\n".join(sorted(names))
    return hashlib.sha1(s.encode("utf-8")).hexdigest()


async def scan_pages(
    page,
    conn: sqlite3.Connection,
    dataset_id: int,
    base_url: str,
    start_page: int,
    use_no_new_streak: bool,
    no_new_streak_start: int = 0,
    stop_at_page: Optional[int] = None,
) -> Tuple[int, int]:
    """
    Scan forward from start_page.

    - If use_no_new_streak=True: increments streak on NEW=0 and stops at MAX_PAGES_WITH_NO_NEW_PDFS
      (DISCOVERY behavior: find the end).
    - If use_no_new_streak=False: streak is NOT incremented/reset by NEW=0
      (REWALK behavior: do not "die" on re-walks).

    stop_at_page:
      - If provided, scanning stops once page_num >= stop_at_page (inclusive).
      - This is what makes "rewalk-to-frontier then continue" possible in a single run.
    """
    mode = "DISCOVERY" if use_no_new_streak else "REWALK"
    log(f"[DS {dataset_id}] {mode} scan start at page {start_page} (streak={no_new_streak_start})")

    pages_no_new = int(no_new_streak_start)
    page_num = int(start_page)
    last_scanned = page_num - 1

    while True:
        if page_num > MAX_PAGES_HARD_CAP:
            log(f"[DS {dataset_id}] HARD CAP reached at page {page_num}. Stopping.")
            conn.commit()
            break

        page_url = base_url.format(page_num)
        log(f"[DS {dataset_id}] Scanning page {page_num}")

        pdfs, err = await scrape_page_for_pdfs(page, page_url)

        pdf_found_total = len(pdfs)
        efta_found = 0
        new_this_page = 0

        status = "ok"
        error_text = None
        page_hash: Optional[str] = None

        # For fingerprint we use EFTA filenames only (matches your tool focus)
        efta_names: List[str] = []

        if err is not None:
            status = "error"
            error_text = err
            log(f"[DS {dataset_id}] ERROR scanning page {page_num}: {err}")
        else:
            # Filter to EFTA only (match ripper)
            for filename, full_url in pdfs:
                n = extract_file_num(filename)
                if n is None:
                    continue
                efta_found += 1
                efta_names.append(filename)
                if upsert_file(conn, dataset_id, filename, n, full_url, page_num):
                    new_this_page += 1

            page_hash = fingerprint_filenames(efta_names) if efta_names else fingerprint_filenames([])

            log(f"[DS {dataset_id}] Found {pdf_found_total} PDFs ({efta_found} EFTA) on page {page_num}")

            # ----- streak logic (only in DISCOVERY) -----
            if use_no_new_streak:
                if new_this_page == 0:
                    pages_no_new += 1
                    log(f"[DS {dataset_id}] No NEW PDFs on page {page_num} (streak={pages_no_new}/{MAX_PAGES_WITH_NO_NEW_PDFS})")
                else:
                    pages_no_new = 0
                    log(f"[DS {dataset_id}] NEW PDFs discovered on page {page_num}: {new_this_page}")
            else:
                # REWALK: do not touch pages_no_new based on NEW=0
                if new_this_page == 0:
                    log(f"[DS {dataset_id}] No NEW PDFs on page {page_num} (rewalk mode; streak ignored)")
                else:
                    log(f"[DS {dataset_id}] NEW PDFs discovered on page {page_num}: {new_this_page}")

            # ----- suspect logic -----
            if MARK_ZERO_PDFS_AS_SUSPECT and pdf_found_total == 0:
                status = "suspect_zero"

            if MARK_REPEAT_PAGES_AS_SUSPECT:
                prev_hash = get_prev_page_hash(conn, dataset_id, page_num)
                if prev_hash is not None and page_hash is not None and page_hash == prev_hash and pdf_found_total > 0:
                    # "served the same slice" style repeat
                    status = "suspect_repeat"

        # persist page record
        upsert_page_result(
            conn,
            dataset_id=dataset_id,
            page_num=page_num,
            status=status,
            pdf_found=pdf_found_total,
            efta_found=efta_found,
            new_files=new_this_page,
            page_hash=page_hash,
            error=error_text,
        )

        # update resume state
        # NOTE: For REWALK, we still advance next_page so "resume" works naturally.
        set_resume_state(
            conn,
            dataset_id=dataset_id,
            next_page=page_num + 1,
            no_new_streak=(pages_no_new if use_no_new_streak else 0),
            last_scan_page=page_num,
        )

        conn.commit()
        last_scanned = page_num

        # stop_at_page (inclusive)
        if stop_at_page is not None and page_num >= stop_at_page:
            log(f"[DS {dataset_id}] Stop-at-page reached: {page_num} (target={stop_at_page}).")
            break

        # discovery stop condition
        if use_no_new_streak and pages_no_new >= MAX_PAGES_WITH_NO_NEW_PDFS:
            log(f"[DS {dataset_id}] Stopping discovery: no NEW PDFs for {MAX_PAGES_WITH_NO_NEW_PDFS} consecutive pages.")
            break

        page_num += 1
        await asyncio.sleep(SLEEP_BETWEEN_PAGES)

    return last_scanned, pages_no_new


async def repair_pages_only(
    page,
    conn: sqlite3.Connection,
    dataset_id: int,
    base_url: str,
    pages_to_repair: List[int],
) -> None:
    """
    Re-scan only suspect/error pages and update DB rows in-place.
    """
    if not pages_to_repair:
        log(f"[DS {dataset_id}] No suspect/error pages to repair.")
        return

    log(f"[DS {dataset_id}] Repair mode ΓÇö pages to repair: {len(pages_to_repair)}")

    for page_num in pages_to_repair:
        page_url = base_url.format(page_num)
        log(f"[DS {dataset_id}] [REPAIR] Scanning page {page_num}")

        pdfs, err = await scrape_page_for_pdfs(page, page_url)

        pdf_found_total = len(pdfs)
        efta_found = 0
        new_this_page = 0

        status = "ok"
        error_text = None
        efta_names: List[str] = []

        if err is not None:
            status = "error"
            error_text = err
            log(f"[DS {dataset_id}] [REPAIR] ERROR page {page_num}: {err}")
            page_hash = fingerprint_filenames([])
        else:
            for filename, full_url in pdfs:
                n = extract_file_num(filename)
                if n is None:
                    continue
                efta_found += 1
                efta_names.append(filename)
                if upsert_file(conn, dataset_id, filename, n, full_url, page_num):
                    new_this_page += 1

            page_hash = fingerprint_filenames(efta_names) if efta_names else fingerprint_filenames([])

            log(f"[DS {dataset_id}] [REPAIR] Found {pdf_found_total} PDFs ({efta_found} EFTA); NEW={new_this_page}")

            if MARK_ZERO_PDFS_AS_SUSPECT and pdf_found_total == 0:
                status = "suspect_zero"

            if MARK_REPEAT_PAGES_AS_SUSPECT:
                prev_hash = get_prev_page_hash(conn, dataset_id, page_num)
                if prev_hash is not None and page_hash == prev_hash and pdf_found_total > 0:
                    status = "suspect_repeat"

        upsert_page_result(
            conn,
            dataset_id=dataset_id,
            page_num=page_num,
            status=status,
            pdf_found=pdf_found_total,
            efta_found=efta_found,
            new_files=new_this_page,
            page_hash=page_hash,
            error=error_text,
        )
        conn.commit()
        await asyncio.sleep(SLEEP_BETWEEN_PAGES)

    log(f"[DS {dataset_id}] Repair pass complete.")


async def rewalk_then_discover(
    page,
    conn: sqlite3.Connection,
    dataset_id: int,
    base_url: str,
    start_page: int,
    rewalk_end: Optional[int] = None,
) -> None:
    """
    Two-phase single run:
      Phase A: REWALK (no-new streak ignored) from start_page -> rewalk_end (default DB frontier)
      Phase B: DISCOVERY (no-new streak enabled) from max(start_page, rewalk_end+1) -> end by streak
    """
    frontier = get_frontier_page(conn, dataset_id)
    end_page = rewalk_end if (rewalk_end is not None and rewalk_end >= 1) else frontier

    if end_page < 1:
        # nothing "known"; just do discovery from start_page
        log(f"[DS {dataset_id}] No frontier yet ΓÇö skipping rewalk; starting discovery at page {start_page}")
        await scan_pages(
            page=page,
            conn=conn,
            dataset_id=dataset_id,
            base_url=base_url,
            start_page=start_page,
            use_no_new_streak=True,
            no_new_streak_start=0,
            stop_at_page=None,
        )
        return

    log(f"[DS {dataset_id}] Two-phase run: REWALK {start_page} -> {end_page}, then DISCOVERY from {max(start_page, end_page+1)}")

    # Phase A
    if end_page >= start_page:
        await scan_pages(
            page=page,
            conn=conn,
            dataset_id=dataset_id,
            base_url=base_url,
            start_page=start_page,
            use_no_new_streak=False,
            no_new_streak_start=0,
            stop_at_page=end_page,
        )

    # Phase B
    discover_start = max(start_page, end_page + 1)
    await scan_pages(
        page=page,
        conn=conn,
        dataset_id=dataset_id,
        base_url=base_url,
        start_page=discover_start,
        use_no_new_streak=True,
        no_new_streak_start=0,
        stop_at_page=None,
    )


# ------------------ UI ------------------

def ui_pick_dataset() -> int:
    print("\nAvailable datasets:", ", ".join(str(n) for n in sorted(DATASETS.keys())))
    while True:
        raw = input("Pick dataset (1-12): ").strip()
        try:
            n = int(raw)
            if n in DATASETS:
                return n
        except ValueError:
            pass
        print("Invalid dataset. Try again.")


def ui_confirm(prompt: str) -> bool:
    raw = input(f"{prompt} [y/N]: ").strip().lower()
    return raw in {"y", "yes"}


def ui_pick_action() -> str:
    print("\nScan options:")
    print("  1) Resume DISCOVERY scan (continue from resume next_page; uses no-new streak)")
    print("  2) Full REWALK -> frontier, then continue DISCOVERY (single run)  [recommended for rewalk/resume]")
    print("  3) Repair suspect/error pages only (0 PDFs / repeat / errors)")
    print("  4) Rewalk custom range, then continue DISCOVERY (single run)")
    print("  5) Show DB stats")
    print("  6) Exit")

    while True:
        raw = input("Choose [1]: ").strip() or "1"
        if raw in {"1", "2", "3", "4", "5", "6"}:
            return raw
        print("Invalid option.")


def ui_int(prompt: str, default: Optional[int] = None, min_v: Optional[int] = None) -> Optional[int]:
    """
    Returns int or None (if user enters blank and default is None).
    """
    while True:
        suffix = f" [{default}]" if default is not None else ""
        raw = input(f"{prompt}{suffix}: ").strip()
        if raw == "":
            return default
        try:
            v = int(raw)
            if min_v is not None and v < min_v:
                print(f"Value must be >= {min_v}")
                continue
            return v
        except ValueError:
            print("Enter an integer or press ENTER.")


# ------------------ Main ------------------

async def run_dataset(dataset_id: int, headless: bool) -> None:
    cfg = DATASETS[dataset_id]
    out_dir = cfg["out_dir"]
    os.makedirs(out_dir, exist_ok=True)

    db_path = db_path_for_dataset(out_dir, cfg["db_file"])
    conn = connect_db(db_path)
    init_db(conn, dataset_id)

    base_url = cfg["base_url"]

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=headless, slow_mo=25)

        context = None
        page = None

        try:
            # DOJ auth context
            context, page = await create_fresh_context(browser, base_url.format(1))

            while True:
                action = ui_pick_action()

                if action == "6":
                    break

                if action == "5":
                    print_stats(conn, dataset_id)
                    continue

                if action == "1":
                    next_page, streak, _last = get_resume_state(conn, dataset_id)
                    await scan_pages(
                        page=page,
                        conn=conn,
                        dataset_id=dataset_id,
                        base_url=base_url,
                        start_page=next_page,
                        use_no_new_streak=True,
                        no_new_streak_start=streak,
                        stop_at_page=None,
                    )
                    continue

                if action == "2":
                    frontier = get_frontier_page(conn, dataset_id)
                    start = ui_int("Start page", default=1, min_v=1) or 1
                    log(f"[DS {dataset_id}] Full REWALK->frontier ({frontier}) then DISCOVERY starting at {max(start, frontier+1)}")
                    await rewalk_then_discover(
                        page=page,
                        conn=conn,
                        dataset_id=dataset_id,
                        base_url=base_url,
                        start_page=start,
                        rewalk_end=None,  # default frontier
                    )
                    continue

                if action == "3":
                    suspects = list_suspect_pages(conn, dataset_id)
                    if not suspects:
                        log(f"[DS {dataset_id}] No suspect/error pages currently recorded.")
                        continue

                    print(f"\nSuspect/error pages: {len(suspects)}")
                    show = suspects[:30]
                    print("First pages:", ", ".join(map(str, show)) + (" ..." if len(suspects) > 30 else ""))

                    limit = input("Repair how many? (ENTER = all): ").strip()
                    to_repair = suspects
                    if limit:
                        try:
                            n = int(limit)
                            if n > 0:
                                to_repair = suspects[:n]
                        except ValueError:
                            pass

                    await repair_pages_only(
                        page=page,
                        conn=conn,
                        dataset_id=dataset_id,
                        base_url=base_url,
                        pages_to_repair=to_repair,
                    )
                    continue

                if action == "4":
                    frontier = get_frontier_page(conn, dataset_id)
                    start = ui_int("Start page", default=1, min_v=1) or 1
                    end_default = frontier if frontier >= 1 else start
                    end = ui_int("Rewalk end page (handoff to discovery after this)", default=end_default, min_v=1) or end_default

                    if end < start:
                        print("End page < start page; swapping.")
                        start, end = end, start

                    await rewalk_then_discover(
                        page=page,
                        conn=conn,
                        dataset_id=dataset_id,
                        base_url=base_url,
                        start_page=start,
                        rewalk_end=end,
                    )
                    continue

        finally:
            try:
                if page is not None:
                    await page.close()
            except Exception:
                pass
            try:
                if context is not None:
                    await context.close()
            except Exception:
                pass
            try:
                await browser.close()
            except Exception:
                pass
            conn.close()

    log(f"[DS {dataset_id}] DONE.")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Epstein DOJ SQLite index scanner (auth + resume + rewalk + repair).")
    p.add_argument("--headless", action="store_true", help="Headless browser (NOT recommended if robot check appears).")
    p.add_argument("--dataset", type=int, default=0, help="Dataset number 1-12 (if omitted, UI prompt is used).")
    return p.parse_args()


async def main():
    args = parse_args()
    dataset_id = args.dataset if args.dataset in DATASETS else ui_pick_dataset()
    log(f"Selected dataset: {dataset_id}")
    await run_dataset(dataset_id, headless=args.headless)


if __name__ == "__main__":
    asyncio.run(main())
