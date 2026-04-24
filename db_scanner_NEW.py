#!/usr/bin/env python3
"""
db_scanner.py / epstein_sql_indexer.py

SQLite-backed DOJ Epstein dataset scanner/indexer updated for EpRip v3-style
SQLite compatibility.

What this does:
- Scans DOJ dataset listing pages and records EFTA PDF URLs.
- Writes an EpRip-compatible SQLite index:
    meta(key, value_json)
    files(filename, url, page, downloaded, downloaded_at, bytes, sha256,
          attempts, skipped, skip_reason, last_error, poison_hits,
          poison_refreshes, first_seen, last_seen, raw_json)
- Keeps scanner-only intelligence tables:
    pages(dataset_id, page_num, scanned_at, status, pdf_found, efta_found,
          new_files, page_hash, error)
    resume_state(dataset_id, next_page, no_new_streak, last_scan_at, last_scan_page)
- Auto-clicks DOJ age/robot gates when possible.
- Closes the visible auth page after authentication and scans via a Playwright APIRequestContext.
- Re-opens a visible page only when scan auth needs refreshing.
- Supports resume, rewalk, discovery, and suspect-page repair.
- Keeps sha256 as a legacy/schema compatibility field, but does NOT compute it.

Drop this in the same project root as EpRip.py and run:
    python3 db_scanner.py
or:
    python3 db_scanner.py --dataset 10
"""

import os
import re
import sys
import json
import time
import random
import argparse
import asyncio
import sqlite3
import hashlib
from datetime import datetime
from typing import Any, Dict, Optional, List, Tuple
from urllib.parse import urljoin, urlparse

from playwright.async_api import async_playwright
from playwright.async_api import TimeoutError as PWTimeoutError

# ================= CONFIG =================

BASE_SITE = "https://www.justice.gov"

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

SLEEP_BETWEEN_PAGES = 0.5

MAX_PAGES_WITH_NO_NEW_PDFS = 300
MAX_PAGES_HARD_CAP = 200000

MAX_SCAN_PAGE_FETCH_RETRIES = 3
MAX_SCAN_PAGE_HARD_FAILURES = 8
SCAN_PAGE_HARD_FAILURE_COOLDOWN = 20.0

RETRY_BACKOFF_BASE = 0.8
RETRY_BACKOFF_CAP = 8.0
RETRY_BACKOFF_JITTER = 0.35

MARK_ZERO_PDFS_AS_SUSPECT = True
MARK_REPEAT_PAGES_AS_SUSPECT = True

PAGE_GOTO_TIMEOUT_MS = 120000
AUTH_WAIT_SECONDS = 600

AGE_GATE_BLOCK = "#age-verify-block"
AGE_YES_BTN = "#age-button-yes"
ROBOT_BTN = "input.usa-button[value='I am not a robot'], input.usa-button[onclick*='reauth']"
DATASET_LIST_PDF_LINKS = "div.block-usdoj-external-files-block a[href$='.pdf']"

AUTH_SLEEP_AFTER_GOTO = 1.5
AUTH_SLEEP_AFTER_ROBOT_CLICK = 1.0
AUTH_SLEEP_AFTER_AGE_CLICK = 0.6
AUTH_SLEEP_AFTER_LIST_VISIBLE = 0.8
AUTH_SESSION_SETTLE_SECONDS = 1.0
KEEP_AUTH_PAGE_OPEN_SECONDS = 0.0
CLOSE_AUTH_PAGE_AFTER_AUTH = True
MAX_SCAN_AUTH_REFRESH_RETRIES = 3
SCAN_AUTH_REFRESH_COOLDOWN = 20.0

# =========================================


def now_ts() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def iso_now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def log(msg: str) -> None:
    line = f"[{now_ts()}] {msg}"
    print(line)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def backoff_sleep_seconds(attempt_num: int) -> float:
    base = min(RETRY_BACKOFF_CAP, RETRY_BACKOFF_BASE * (2 ** max(0, attempt_num - 1)))
    return base + (base * random.uniform(0.0, RETRY_BACKOFF_JITTER))


def is_retryable_playwright_error(msg: str) -> bool:
    m = (msg or "").lower()
    signals = [
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
    return any(s in m for s in signals)


def extract_file_num(filename: str) -> Optional[int]:
    m = re.match(r"EFTA0*(\d+)\.pdf$", filename, re.IGNORECASE)
    if not m:
        return None
    return int(m.group(1))


def is_valid_epstein_pdf_url(full_url: str) -> bool:
    u = full_url.lower()
    return ("/epstein/files/" in u) and u.endswith(".pdf")


def bytes_path_looks_like_pdf(path: str) -> bool:
    try:
        with open(path, "rb") as f:
            return f.read(5) == b"%PDF-"
    except Exception:
        return False


def db_path_for_dataset(out_dir: str, db_file: str) -> str:
    return os.path.join(out_dir, db_file)


def unique_path_variant(path: str, suffix: str) -> str:
    root, ext = os.path.splitext(path)
    candidate = f"{root}{suffix}{ext}"
    if not os.path.exists(candidate):
        return candidate

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    return f"{root}{suffix}_{ts}{ext}"


def discover_index_files(dataset_id: int, out_dir: str) -> Dict[str, List[str]]:
    found = {"sqlite": [], "json": []}
    if not os.path.isdir(out_dir):
        return found

    for name in sorted(os.listdir(out_dir)):
        path = os.path.join(out_dir, name)
        if not os.path.isfile(path):
            continue
        lower = name.lower()
        if lower.endswith(".json"):
            found["json"].append(path)
        elif lower.endswith(".sqlite") or lower.endswith(".sqlite3") or lower.endswith(".db"):
            found["sqlite"].append(path)
    return found


def count_pdf_files(out_dir: str) -> int:
    if not os.path.isdir(out_dir):
        return 0
    count = 0
    for name in os.listdir(out_dir):
        path = os.path.join(out_dir, name)
        if os.path.isfile(path) and name.lower().endswith(".pdf"):
            count += 1
    return count


def connect_db(db_path: str) -> sqlite3.Connection:
    os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA temp_store=MEMORY;")
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn


def table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    return row is not None


def table_columns(conn: sqlite3.Connection, table: str) -> List[str]:
    if not table_exists(conn, table):
        return []
    return [r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]


def ep_entry_to_row(filename: str, entry: Dict[str, Any]) -> Tuple[Any, ...]:
    return (
        filename,
        entry.get("url"),
        entry.get("page"),
        1 if entry.get("downloaded") else 0,
        entry.get("downloaded_at"),
        entry.get("bytes"),
        entry.get("sha256"),  # legacy field: preserved, not computed by scanner
        int(entry.get("attempts", 0) or 0),
        1 if entry.get("skipped") else 0,
        entry.get("skip_reason"),
        entry.get("last_error"),
        int(entry.get("poison_hits", 0) or 0),
        int(entry.get("poison_refreshes", 0) or 0),
        entry.get("first_seen"),
        entry.get("last_seen"),
        json.dumps(entry, sort_keys=True),
    )


def make_ep_entry(
    *,
    filename: str,
    url: str,
    page_num: int,
    first_seen: Optional[str] = None,
    last_seen: Optional[str] = None,
    downloaded: bool = False,
    downloaded_at: Optional[str] = None,
    num_bytes: Optional[int] = None,
    sha256: Optional[str] = None,
    attempts: int = 0,
    skipped: bool = False,
    skip_reason: Optional[str] = None,
    last_error: Optional[str] = None,
    poison_hits: int = 0,
    poison_refreshes: int = 0,
    dataset_id: Optional[int] = None,
    file_num: Optional[int] = None,
) -> Dict[str, Any]:
    now = iso_now()
    entry: Dict[str, Any] = {
        "url": url,
        "first_seen": first_seen or now,
        "last_seen": last_seen or now,
        "page": page_num,
        "downloaded": bool(downloaded),
        "downloaded_at": downloaded_at,
        "sha256": sha256,  # kept only for EpRip legacy/schema compatibility
        "bytes": num_bytes,
        "attempts": int(attempts or 0),
        "last_error": last_error,
        "poison_hits": int(poison_hits or 0),
        "poison_refreshes": int(poison_refreshes or 0),
        "skipped": bool(skipped),
        "skip_reason": skip_reason,
    }

    # Extra harmless fields in raw_json are useful for analysis without affecting EpRip.
    if dataset_id is not None:
        entry["dataset"] = dataset_id
    if file_num is not None:
        entry["file_num"] = file_num

    return entry


def hydrate_entry_from_disk(entry: Dict[str, Any], out_dir: str, filename: str) -> bool:
    """
    If the PDF already exists and looks real, mark downloaded and set byte count.
    Does NOT compute sha256.
    """
    out_path = os.path.join(out_dir, filename)
    if not os.path.exists(out_path):
        return False
    if not bytes_path_looks_like_pdf(out_path):
        return False

    changed = False
    if not entry.get("downloaded"):
        entry["downloaded"] = True
        entry["downloaded_at"] = entry.get("downloaded_at") or iso_now()
        entry["last_error"] = None
        changed = True
    try:
        size = os.path.getsize(out_path)
        if entry.get("bytes") != size:
            entry["bytes"] = size
            changed = True
    except Exception:
        pass
    return changed


def ensure_ep_rip_schema(conn: sqlite3.Connection) -> None:
    """
    EpRip-compatible core schema.
    This intentionally matches EpRip's SQLite loader/writer expectations.
    """
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS meta (
            key TEXT PRIMARY KEY,
            value_json TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS files (
            filename TEXT PRIMARY KEY,
            url TEXT,
            page INTEGER,
            downloaded INTEGER,
            downloaded_at TEXT,
            bytes INTEGER,
            sha256 TEXT,
            attempts INTEGER,
            skipped INTEGER,
            skip_reason TEXT,
            last_error TEXT,
            poison_hits INTEGER,
            poison_refreshes INTEGER,
            first_seen TEXT,
            last_seen TEXT,
            raw_json TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_files_downloaded ON files(downloaded);
        CREATE INDEX IF NOT EXISTS idx_files_page ON files(page);
        CREATE INDEX IF NOT EXISTS idx_files_skipped ON files(skipped);
        """
    )


def ensure_scanner_extra_schema(conn: sqlite3.Connection) -> None:
    """
    Scanner-only extras. EpRip ignores these; db_scanner uses them for page status,
    repeat-page detection, suspect repairs, and resume/discovery state.
    """
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS pages (
            dataset_id INTEGER NOT NULL,
            page_num INTEGER NOT NULL,
            scanned_at TEXT,
            status TEXT NOT NULL,
            pdf_found INTEGER NOT NULL DEFAULT 0,
            efta_found INTEGER NOT NULL DEFAULT 0,
            new_files INTEGER NOT NULL DEFAULT 0,
            page_hash TEXT,
            error TEXT,
            PRIMARY KEY (dataset_id, page_num)
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
        """
    )


def migrate_legacy_schema_if_needed(conn: sqlite3.Connection, db_path: str, dataset_id: int, out_dir: str) -> None:
    """
    Old db_scanner schema used:
        meta(key, value)
        files(dataset_id, filename, file_num, url, first_seen, last_seen, last_page)
    EpRip v3 expects:
        meta(key, value_json)
        files(filename, url, page, downloaded, downloaded_at, bytes, sha256,
              attempts, skipped, skip_reason, last_error, poison_hits,
              poison_refreshes, first_seen, last_seen, raw_json)

    This migrates old scanner tables safely by renaming them to *_legacy_TIMESTAMP
    and inserting converted rows into the new EpRip-compatible tables.
    """
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")

    # meta migration
    meta_cols = table_columns(conn, "meta")
    legacy_meta_table = None
    if meta_cols and "value_json" not in meta_cols:
        legacy_meta_table = f"meta_legacy_{ts}"
        log(f"[DS {dataset_id}] Migrating legacy meta table -> {legacy_meta_table}")
        conn.execute(f"ALTER TABLE meta RENAME TO {legacy_meta_table}")

    # files migration
    file_cols = table_columns(conn, "files")
    required_file_cols = {
        "filename", "url", "page", "downloaded", "downloaded_at", "bytes",
        "sha256", "attempts", "skipped", "skip_reason", "last_error",
        "poison_hits", "poison_refreshes", "first_seen", "last_seen", "raw_json",
    }
    legacy_files_table = None
    if file_cols and not required_file_cols.issubset(set(file_cols)):
        legacy_files_table = f"files_legacy_{ts}"
        log(f"[DS {dataset_id}] Migrating legacy files table -> {legacy_files_table}")
        conn.execute(f"ALTER TABLE files RENAME TO {legacy_files_table}")

    ensure_ep_rip_schema(conn)

    if legacy_meta_table:
        old_rows = conn.execute(f"SELECT key, value FROM {legacy_meta_table}").fetchall()
        for row in old_rows:
            key = row["key"]
            val = row["value"]
            parsed: Any = val
            if key in {"version", "dataset", "dataset_id", "last_scan_page"}:
                try:
                    parsed = int(val)
                except Exception:
                    parsed = val
            conn.execute(
                """
                INSERT INTO meta(key, value_json) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json
                """,
                (key, json.dumps(parsed, sort_keys=True)),
            )

    if legacy_files_table:
        old_cols = set(table_columns(conn, legacy_files_table))
        select_cols = ", ".join([
            c for c in ["dataset_id", "filename", "file_num", "url", "first_seen", "last_seen", "last_page"]
            if c in old_cols
        ])
        rows = conn.execute(f"SELECT {select_cols} FROM {legacy_files_table}").fetchall()
        migrated = 0
        for row in rows:
            filename = row["filename"]
            if not filename:
                continue
            page_num = int(row["last_page"] or 0) if "last_page" in old_cols else 0
            file_num = row["file_num"] if "file_num" in old_cols else extract_file_num(filename)
            entry = make_ep_entry(
                filename=filename,
                url=row["url"] if "url" in old_cols else "",
                page_num=page_num,
                first_seen=row["first_seen"] if "first_seen" in old_cols else None,
                last_seen=row["last_seen"] if "last_seen" in old_cols else None,
                dataset_id=dataset_id,
                file_num=file_num,
            )
            hydrate_entry_from_disk(entry, out_dir, filename)
            conn.execute(
                """
                INSERT INTO files(
                    filename, url, page, downloaded, downloaded_at, bytes, sha256,
                    attempts, skipped, skip_reason, last_error, poison_hits,
                    poison_refreshes, first_seen, last_seen, raw_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(filename) DO UPDATE SET
                    url=excluded.url,
                    page=excluded.page,
                    downloaded=excluded.downloaded,
                    downloaded_at=excluded.downloaded_at,
                    bytes=excluded.bytes,
                    sha256=excluded.sha256,
                    attempts=excluded.attempts,
                    skipped=excluded.skipped,
                    skip_reason=excluded.skip_reason,
                    last_error=excluded.last_error,
                    poison_hits=excluded.poison_hits,
                    poison_refreshes=excluded.poison_refreshes,
                    first_seen=excluded.first_seen,
                    last_seen=excluded.last_seen,
                    raw_json=excluded.raw_json
                """,
                ep_entry_to_row(filename, entry),
            )
            migrated += 1
        log(f"[DS {dataset_id}] Migrated legacy file rows: {migrated}")

    conn.commit()


def meta_upsert(conn: sqlite3.Connection, key: str, value: Any) -> None:
    conn.execute(
        """
        INSERT INTO meta(key, value_json) VALUES (?, ?)
        ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json
        """,
        (key, json.dumps(value, sort_keys=True)),
    )


def init_db(conn: sqlite3.Connection, dataset_id: int, out_dir: str, db_path: str) -> None:
    migrate_legacy_schema_if_needed(conn, db_path, dataset_id, out_dir)
    ensure_ep_rip_schema(conn)
    ensure_scanner_extra_schema(conn)

    meta_upsert(conn, "dataset", dataset_id)
    meta_upsert(conn, "version", 3)
    conn.execute(
        """
        INSERT INTO meta(key, value_json) VALUES ('created_at', ?)
        ON CONFLICT(key) DO NOTHING
        """,
        (json.dumps(iso_now()),),
    )
    conn.execute(
        """
        INSERT INTO meta(key, value_json) VALUES ('last_scan_at', ?)
        ON CONFLICT(key) DO NOTHING
        """,
        (json.dumps(None),),
    )
    conn.execute(
        """
        INSERT INTO meta(key, value_json) VALUES ('last_scan_page', ?)
        ON CONFLICT(key) DO NOTHING
        """,
        (json.dumps(0),),
    )
    meta_upsert(conn, "scanner", {
        "name": "db_scanner",
        "compatibility": "EpRip v3 SQLite index",
        "sha256_policy": "column/key retained for legacy compatibility; scanner does not compute hashes",
    })

    conn.execute(
        """
        INSERT OR IGNORE INTO resume_state(dataset_id, next_page, no_new_streak, last_scan_at, last_scan_page)
        VALUES(?, 1, 0, NULL, 0)
        """,
        (dataset_id,),
    )
    conn.commit()


def normalize_json_entry_for_sqlite(
    filename: str,
    raw_entry: Dict[str, Any],
    dataset_id: int,
    out_dir: str,
    hydrate_from_disk: bool,
) -> Optional[Dict[str, Any]]:
    if not filename or not isinstance(raw_entry, dict):
        return None

    file_num = raw_entry.get("file_num")
    if file_num is None:
        file_num = extract_file_num(filename)

    page_num_raw = raw_entry.get("page")
    if page_num_raw is None:
        page_num_raw = raw_entry.get("last_page")
    try:
        page_num = int(page_num_raw or 0)
    except (TypeError, ValueError):
        page_num = 0

    first_seen = raw_entry.get("first_seen") or iso_now()
    last_seen = raw_entry.get("last_seen") or first_seen

    entry = dict(raw_entry)
    entry["url"] = entry.get("url") or ""
    entry["page"] = page_num
    entry["downloaded"] = bool(entry.get("downloaded"))
    entry["downloaded_at"] = entry.get("downloaded_at")
    entry["bytes"] = entry.get("bytes")
    entry["sha256"] = entry.get("sha256")
    entry["attempts"] = int(entry.get("attempts", 0) or 0)
    entry["skipped"] = bool(entry.get("skipped"))
    entry["skip_reason"] = entry.get("skip_reason")
    entry["last_error"] = entry.get("last_error")
    entry["poison_hits"] = int(entry.get("poison_hits", 0) or 0)
    entry["poison_refreshes"] = int(entry.get("poison_refreshes", 0) or 0)
    entry["first_seen"] = first_seen
    entry["last_seen"] = last_seen
    entry["dataset"] = dataset_id
    if file_num is not None:
        entry["file_num"] = file_num

    if hydrate_from_disk:
        hydrate_entry_from_disk(entry, out_dir, filename)

    return entry


def import_json_index_into_sqlite(
    json_path: str,
    db_path: str,
    dataset_id: int,
    out_dir: str,
    hydrate_from_disk: bool,
) -> Dict[str, int]:
    with open(json_path, "r", encoding="utf-8") as fh:
        idx = json.load(fh)

    if not isinstance(idx, dict):
        raise RuntimeError(f"Top-level JSON object expected in {json_path}")

    meta = idx.get("meta", {})
    files = idx.get("files", {})
    if meta is not None and not isinstance(meta, dict):
        raise RuntimeError(f"'meta' object expected in {json_path}")
    if not isinstance(files, dict):
        raise RuntimeError(f"'files' object expected in {json_path}")

    created_new_db = not os.path.exists(db_path)
    conn = connect_db(db_path)
    try:
        init_db(conn, dataset_id, out_dir, db_path)

        stats = {
            "json_entries_seen": 0,
            "rows_added": 0,
            "rows_skipped_existing": 0,
            "rows_skipped_invalid": 0,
            "created_new_db": 1 if created_new_db else 0,
        }

        for filename, raw_entry in files.items():
            stats["json_entries_seen"] += 1
            if fetch_existing_file_entry(conn, filename) is not None:
                stats["rows_skipped_existing"] += 1
                continue

            entry = normalize_json_entry_for_sqlite(
                filename=filename,
                raw_entry=raw_entry,
                dataset_id=dataset_id,
                out_dir=out_dir,
                hydrate_from_disk=hydrate_from_disk,
            )
            if entry is None:
                stats["rows_skipped_invalid"] += 1
                continue

            conn.execute(
                """
                INSERT INTO files(
                    filename, url, page, downloaded, downloaded_at, bytes, sha256,
                    attempts, skipped, skip_reason, last_error, poison_hits,
                    poison_refreshes, first_seen, last_seen, raw_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(filename) DO NOTHING
                """,
                ep_entry_to_row(filename, entry),
            )
            stats["rows_added"] += 1

        meta_upsert(conn, "dataset", dataset_id)
        meta_upsert(conn, "scanner_import", {
            "source": os.path.abspath(json_path),
            "imported_at": iso_now(),
            "json_entries_seen": stats["json_entries_seen"],
            "rows_added": stats["rows_added"],
            "rows_skipped_existing": stats["rows_skipped_existing"],
            "rows_skipped_invalid": stats["rows_skipped_invalid"],
            "hydrate_from_disk": bool(hydrate_from_disk),
            "merge_policy": "add_missing_only_by_filename",
        })
        # Preserve existing scanner meta; import source metadata only if the key does not already exist.
        for key, value in (meta or {}).items():
            row = conn.execute("SELECT 1 FROM meta WHERE key=?", (key,)).fetchone()
            if row is None:
                meta_upsert(conn, key, value)

        conn.commit()
        return stats
    finally:
        conn.close()


def get_resume_state(conn: sqlite3.Connection, dataset_id: int) -> Tuple[int, int, int]:
    row = conn.execute(
        "SELECT next_page, no_new_streak, last_scan_page FROM resume_state WHERE dataset_id=?",
        (dataset_id,),
    ).fetchone()
    if not row:
        return 1, 0, 0
    return int(row["next_page"]), int(row["no_new_streak"]), int(row["last_scan_page"])


def set_resume_state(conn: sqlite3.Connection, dataset_id: int, next_page: int, no_new_streak: int, last_scan_page: int) -> None:
    now = iso_now()
    conn.execute(
        """
        UPDATE resume_state
        SET next_page=?, no_new_streak=?, last_scan_at=?, last_scan_page=?
        WHERE dataset_id=?
        """,
        (next_page, no_new_streak, now, last_scan_page, dataset_id),
    )
    meta_upsert(conn, "last_scan_at", now)
    meta_upsert(conn, "last_scan_page", last_scan_page)


def get_frontier_page(conn: sqlite3.Connection, dataset_id: int) -> int:
    row = conn.execute(
        "SELECT MAX(page_num) AS m FROM pages WHERE dataset_id=?",
        (dataset_id,),
    ).fetchone()
    return int(row["m"]) if row and row["m"] is not None else 0


def get_prev_page_hash(conn: sqlite3.Connection, dataset_id: int, page_num: int) -> Optional[str]:
    if page_num <= 1:
        return None
    row = conn.execute(
        "SELECT page_hash FROM pages WHERE dataset_id=? AND page_num=?",
        (dataset_id, page_num - 1),
    ).fetchone()
    return row["page_hash"] if row else None


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
        (dataset_id, page_num, iso_now(), status, int(pdf_found), int(efta_found), int(new_files), page_hash, error),
    )


def fetch_existing_file_entry(conn: sqlite3.Connection, filename: str) -> Optional[Dict[str, Any]]:
    row = conn.execute(
        """
        SELECT filename, raw_json, url, page, downloaded, downloaded_at, bytes,
               sha256, attempts, skipped, skip_reason, last_error,
               poison_hits, poison_refreshes, first_seen, last_seen
        FROM files
        WHERE filename=?
        """,
        (filename,),
    ).fetchone()
    if not row:
        return None

    entry: Dict[str, Any]
    try:
        entry = json.loads(row["raw_json"]) if row["raw_json"] else {}
        if not isinstance(entry, dict):
            entry = {}
    except Exception:
        entry = {}

    entry.setdefault("url", row["url"])
    entry.setdefault("page", row["page"])
    entry.setdefault("downloaded", bool(row["downloaded"]))
    entry.setdefault("downloaded_at", row["downloaded_at"])
    entry.setdefault("bytes", row["bytes"])
    entry.setdefault("sha256", row["sha256"])
    entry.setdefault("attempts", row["attempts"] or 0)
    entry.setdefault("skipped", bool(row["skipped"]))
    entry.setdefault("skip_reason", row["skip_reason"])
    entry.setdefault("last_error", row["last_error"])
    entry.setdefault("poison_hits", row["poison_hits"] or 0)
    entry.setdefault("poison_refreshes", row["poison_refreshes"] or 0)
    entry.setdefault("first_seen", row["first_seen"])
    entry.setdefault("last_seen", row["last_seen"])
    return entry


def upsert_file(
    conn: sqlite3.Connection,
    dataset_id: int,
    filename: str,
    file_num: Optional[int],
    url: str,
    page_num: int,
    out_dir: str,
    hydrate_from_disk: bool,
) -> bool:
    """
    Insert/update an EpRip-style files row.
    Returns True if newly inserted, False if already existed.
    """
    existing = fetch_existing_file_entry(conn, filename)
    now = iso_now()
    inserted = existing is None

    if existing is None:
        entry = make_ep_entry(
            filename=filename,
            url=url,
            page_num=page_num,
            dataset_id=dataset_id,
            file_num=file_num,
        )
    else:
        entry = dict(existing)
        entry["url"] = url or entry.get("url")
        entry["page"] = page_num
        entry["last_seen"] = now
        entry.setdefault("first_seen", now)
        entry.setdefault("downloaded", False)
        entry.setdefault("downloaded_at", None)
        entry.setdefault("sha256", None)
        entry.setdefault("bytes", None)
        entry.setdefault("attempts", 0)
        entry.setdefault("last_error", None)
        entry.setdefault("poison_hits", 0)
        entry.setdefault("poison_refreshes", 0)
        entry.setdefault("skipped", False)
        entry.setdefault("skip_reason", None)
        entry["dataset"] = dataset_id
        if file_num is not None:
            entry["file_num"] = file_num

    if hydrate_from_disk:
        hydrate_entry_from_disk(entry, out_dir, filename)

    conn.execute(
        """
        INSERT INTO files(
            filename, url, page, downloaded, downloaded_at, bytes, sha256,
            attempts, skipped, skip_reason, last_error, poison_hits,
            poison_refreshes, first_seen, last_seen, raw_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(filename) DO UPDATE SET
            url=excluded.url,
            page=excluded.page,
            downloaded=excluded.downloaded,
            downloaded_at=excluded.downloaded_at,
            bytes=excluded.bytes,
            sha256=excluded.sha256,
            attempts=excluded.attempts,
            skipped=excluded.skipped,
            skip_reason=excluded.skip_reason,
            last_error=excluded.last_error,
            poison_hits=excluded.poison_hits,
            poison_refreshes=excluded.poison_refreshes,
            first_seen=excluded.first_seen,
            last_seen=excluded.last_seen,
            raw_json=excluded.raw_json
        """,
        ep_entry_to_row(filename, entry),
    )
    return inserted


def list_suspect_pages(conn: sqlite3.Connection, dataset_id: int, limit: Optional[int] = None) -> List[int]:
    q = """
        SELECT page_num
        FROM pages
        WHERE dataset_id=? AND status IN ('suspect_zero','suspect_repeat','error')
        ORDER BY page_num
    """
    params: List[Any] = [dataset_id]
    if limit is not None:
        q += " LIMIT ?"
        params.append(int(limit))
    return [int(r["page_num"]) for r in conn.execute(q, params).fetchall()]


def print_stats(conn: sqlite3.Connection, dataset_id: int) -> None:
    total_files = conn.execute("SELECT COUNT(*) AS c FROM files").fetchone()["c"]
    downloaded = conn.execute("SELECT COUNT(*) AS c FROM files WHERE downloaded=1").fetchone()["c"]
    skipped = conn.execute("SELECT COUNT(*) AS c FROM files WHERE skipped=1").fetchone()["c"]
    total_pages = conn.execute("SELECT COUNT(*) AS c FROM pages WHERE dataset_id=?", (dataset_id,)).fetchone()["c"]
    total_pdf_refs = conn.execute(
        "SELECT COALESCE(SUM(pdf_found), 0) AS c FROM pages WHERE dataset_id=?",
        (dataset_id,),
    ).fetchone()["c"]
    total_efta_refs = conn.execute(
        "SELECT COALESCE(SUM(efta_found), 0) AS c FROM pages WHERE dataset_id=?",
        (dataset_id,),
    ).fetchone()["c"]
    total_new_discovered = conn.execute(
        "SELECT COALESCE(SUM(new_files), 0) AS c FROM pages WHERE dataset_id=?",
        (dataset_id,),
    ).fetchone()["c"]
    suspect_pages = conn.execute(
        "SELECT COUNT(*) AS c FROM pages WHERE dataset_id=? AND status IN ('suspect_zero','suspect_repeat','error')",
        (dataset_id,),
    ).fetchone()["c"]
    next_page, streak, last_scan_page = get_resume_state(conn, dataset_id)
    frontier = get_frontier_page(conn, dataset_id)

    print("\n=== DB STATS ===")
    print(f"Dataset:                  {dataset_id}")
    print(f"PDFs in index (distinct): {total_files}")
    print(f"PDF refs seen on pages:   {total_pdf_refs}")
    print(f"EFTA refs seen on pages:  {total_efta_refs}")
    print(f"New PDFs discovered:      {total_new_discovered}")
    print(f"Files downloaded:         {downloaded}")
    print(f"Files skipped:            {skipped}")
    print(f"Pages scanned:            {total_pages}")
    print(f"Suspect/error pages:      {suspect_pages}")
    print(f"Frontier max page:        {frontier}")
    print(f"Resume next_page:         {next_page}")
    print(f"Resume no_new_streak:     {streak}")
    print(f"Last scan page:           {last_scan_page}")
    print("================\n")


def print_interrupt_summary(
    conn: sqlite3.Connection,
    dataset_id: int,
    db_path: str,
    initial_files_count: int,
) -> None:
    try:
        conn.commit()
    except Exception:
        pass

    next_page, streak, last_scan_page = get_resume_state(conn, dataset_id)
    total_files = conn.execute("SELECT COUNT(*) AS c FROM files").fetchone()["c"]
    downloaded = conn.execute("SELECT COUNT(*) AS c FROM files WHERE downloaded=1").fetchone()["c"]
    total_pages = conn.execute(
        "SELECT COUNT(*) AS c FROM pages WHERE dataset_id=?",
        (dataset_id,),
    ).fetchone()["c"]
    total_pdf_refs = conn.execute(
        "SELECT COALESCE(SUM(pdf_found), 0) AS c FROM pages WHERE dataset_id=?",
        (dataset_id,),
    ).fetchone()["c"]
    total_efta_refs = conn.execute(
        "SELECT COALESCE(SUM(efta_found), 0) AS c FROM pages WHERE dataset_id=?",
        (dataset_id,),
    ).fetchone()["c"]
    suspect_pages = conn.execute(
        "SELECT COUNT(*) AS c FROM pages WHERE dataset_id=? AND status IN ('suspect_zero','suspect_repeat','error')",
        (dataset_id,),
    ).fetchone()["c"]
    last_scanned_at_row = conn.execute(
        "SELECT scanned_at FROM pages WHERE dataset_id=? AND page_num=?",
        (dataset_id, last_scan_page),
    ).fetchone() if last_scan_page > 0 else None
    last_scanned_at = last_scanned_at_row["scanned_at"] if last_scanned_at_row else None
    new_discovered_this_run = max(0, total_files - initial_files_count)

    print("\n=== INTERRUPT SUMMARY ===")
    print("Shutdown requested with Ctrl+C.")
    print(f"Dataset:                   {dataset_id}")
    print(f"Working DB:                {db_path}")
    print("Pending SQLite work:       committed before shutdown")
    print("Write safety:              page/file writes are committed after each scanned page")
    print(f"Pages recorded:            {total_pages}")
    print(f"PDF refs seen on pages:    {total_pdf_refs}")
    print(f"EFTA refs seen on pages:   {total_efta_refs}")
    print(f"New PDFs discovered:       {new_discovered_this_run}")
    print(f"Files in index:            {total_files}")
    print(f"Files marked downloaded:   {downloaded}")
    print(f"Suspect/error pages:       {suspect_pages}")
    print(f"Last committed page:       {last_scan_page}")
    print(f"Last committed scan time:  {last_scanned_at or 'n/a'}")
    print(f"Resume will start at:      {next_page}")
    print(f"Resume no_new_streak:      {streak}")
    print("Safe to restart now:       yes")
    print("Browser/request contexts:  closing")
    print("=========================\n")


def clear_current_task_cancellation() -> None:
    task = asyncio.current_task()
    if task is None:
        return
    while task.cancelling():
        task.uncancel()


def upsert_existing_entry(conn: sqlite3.Connection, filename: str, entry: Dict[str, Any]) -> None:
    conn.execute(
        """
        INSERT INTO files(
            filename, url, page, downloaded, downloaded_at, bytes, sha256,
            attempts, skipped, skip_reason, last_error, poison_hits,
            poison_refreshes, first_seen, last_seen, raw_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(filename) DO UPDATE SET
            url=excluded.url,
            page=excluded.page,
            downloaded=excluded.downloaded,
            downloaded_at=excluded.downloaded_at,
            bytes=excluded.bytes,
            sha256=excluded.sha256,
            attempts=excluded.attempts,
            skipped=excluded.skipped,
            skip_reason=excluded.skip_reason,
            last_error=excluded.last_error,
            poison_hits=excluded.poison_hits,
            poison_refreshes=excluded.poison_refreshes,
            first_seen=excluded.first_seen,
            last_seen=excluded.last_seen,
            raw_json=excluded.raw_json
        """,
        ep_entry_to_row(filename, entry),
    )


def reconcile_existing_entry_with_disk(entry: Dict[str, Any], out_dir: str, filename: str) -> Dict[str, int]:
    result = {
        "changed": 0,
        "marked_downloaded": 0,
        "flipped_to_false": 0,
        "bytes_updated": 0,
        "already_downloaded": 0,
        "missing_on_disk": 0,
        "invalid_on_disk": 0,
    }

    out_path = os.path.join(out_dir, filename)
    before_downloaded = bool(entry.get("downloaded"))
    before_bytes = entry.get("bytes")

    if not os.path.exists(out_path):
        result["missing_on_disk"] = 1
        if before_downloaded:
            entry["downloaded"] = False
            entry["downloaded_at"] = None
            entry["bytes"] = None
            entry["sha256"] = None
            result["changed"] = 1
            result["flipped_to_false"] = 1
        return result

    if not bytes_path_looks_like_pdf(out_path):
        result["invalid_on_disk"] = 1
        if before_downloaded:
            entry["downloaded"] = False
            entry["downloaded_at"] = None
            entry["bytes"] = None
            entry["sha256"] = None
            result["changed"] = 1
            result["flipped_to_false"] = 1
        return result

    changed = hydrate_entry_from_disk(entry, out_dir, filename)
    if changed:
        result["changed"] = 1
        if not before_downloaded and entry.get("downloaded"):
            result["marked_downloaded"] = 1
        if entry.get("bytes") != before_bytes:
            result["bytes_updated"] = 1
    elif before_downloaded:
        result["already_downloaded"] = 1

    return result


def make_disk_repair_stats() -> Dict[str, int]:
    return {
        "rows_scanned": 0,
        "rows_updated": 0,
        "marked_downloaded": 0,
        "flipped_to_false": 0,
        "bytes_updated": 0,
        "already_downloaded": 0,
        "missing_on_disk": 0,
        "invalid_on_disk": 0,
    }


def repair_downloaded_flags_from_disk(conn: sqlite3.Connection, out_dir: str) -> Dict[str, int]:
    stats = make_disk_repair_stats()

    rows = conn.execute(
        """
        SELECT filename
        FROM files
        ORDER BY filename
        """
    ).fetchall()

    for row in rows:
        filename = row["filename"]
        stats["rows_scanned"] += 1
        existing = fetch_existing_file_entry(conn, filename)
        if existing is None:
            continue

        result = reconcile_existing_entry_with_disk(existing, out_dir, filename)
        stats["marked_downloaded"] += result["marked_downloaded"]
        stats["flipped_to_false"] += result["flipped_to_false"]
        stats["bytes_updated"] += result["bytes_updated"]
        stats["already_downloaded"] += result["already_downloaded"]
        stats["missing_on_disk"] += result["missing_on_disk"]
        stats["invalid_on_disk"] += result["invalid_on_disk"]

        if result["changed"]:
            stats["rows_updated"] += 1
            upsert_existing_entry(conn, filename, existing)

    meta_upsert(conn, "last_disk_repair_at", iso_now())
    conn.commit()
    return stats


def sync_db_from_disk(conn: sqlite3.Connection, dataset_id: int, out_dir: str) -> Dict[str, int]:
    stats = {
        "disk_pdfs_seen": 0,
        "valid_efta_pdfs": 0,
        "new_rows_added": 0,
        "existing_rows_updated": 0,
        "marked_downloaded": 0,
        "flipped_to_false": 0,
        "bytes_updated": 0,
        "already_downloaded": 0,
        "missing_on_disk": 0,
        "invalid_pdf": 0,
        "non_efta_skipped": 0,
    }

    if not os.path.isdir(out_dir):
        return stats

    preexisting_rows = {
        str(row["filename"])
        for row in conn.execute("SELECT filename FROM files").fetchall()
    }

    for name in sorted(os.listdir(out_dir)):
        out_path = os.path.join(out_dir, name)
        if not (os.path.isfile(out_path) and name.lower().endswith(".pdf")):
            continue

        stats["disk_pdfs_seen"] += 1
        if not bytes_path_looks_like_pdf(out_path):
            stats["invalid_pdf"] += 1
            continue

        file_num = extract_file_num(name)
        if file_num is None:
            stats["non_efta_skipped"] += 1
            continue

        stats["valid_efta_pdfs"] += 1
        existing = fetch_existing_file_entry(conn, name)
        if existing is None:
            entry = make_ep_entry(
                filename=name,
                url="",
                page_num=0,
                downloaded=True,
                downloaded_at=iso_now(),
                dataset_id=dataset_id,
                file_num=file_num,
            )
            hydrate_entry_from_disk(entry, out_dir, name)
            upsert_existing_entry(conn, name, entry)
            stats["new_rows_added"] += 1
            continue

    for filename in sorted(preexisting_rows):
        existing = fetch_existing_file_entry(conn, filename)
        if existing is None:
            continue

        result = reconcile_existing_entry_with_disk(existing, out_dir, filename)
        stats["marked_downloaded"] += result["marked_downloaded"]
        stats["flipped_to_false"] += result["flipped_to_false"]
        stats["bytes_updated"] += result["bytes_updated"]
        stats["already_downloaded"] += result["already_downloaded"]
        stats["missing_on_disk"] += result["missing_on_disk"]
        stats["invalid_pdf"] += result["invalid_on_disk"]

        if result["changed"]:
            stats["existing_rows_updated"] += 1
            upsert_existing_entry(conn, filename, existing)

    meta_upsert(conn, "last_disk_sync_at", iso_now())
    conn.commit()
    return stats


def print_startup_disk_sync_summary(stats: Dict[str, int], db_path: str) -> None:
    print("\n=== STARTUP DISK SYNC ===")
    print(f"Working DB:              {db_path}")
    print(f"PDFs found on disk:      {stats['disk_pdfs_seen']}")
    print(f"Valid EFTA PDFs:         {stats['valid_efta_pdfs']}")
    print(f"New DB rows added:       {stats['new_rows_added']}")
    print(f"Existing rows updated:   {stats['existing_rows_updated']}")
    print(f"* marked downloaded:     {stats['marked_downloaded']}")
    print(f"* flipped to false:      {stats['flipped_to_false']}")
    print(f"* byte counts updated:   {stats['bytes_updated']}")
    print(f"Already downloaded:      {stats['already_downloaded']}")
    print(f"Missing on disk:         {stats['missing_on_disk']}")
    print(f"Invalid PDFs skipped:    {stats['invalid_pdf']}")
    print(f"Non-EFTA PDFs skipped:   {stats['non_efta_skipped']}")
    print("========================\n")


def collect_duplicate_groups(rows: List[sqlite3.Row], key_name: str) -> List[Tuple[str, List[str]]]:
    groups: Dict[str, List[str]] = {}
    for row in rows:
        value = row[key_name]
        if value is None:
            continue
        key = str(value).strip()
        if not key:
            continue
        groups.setdefault(key, []).append(str(row["filename"]))
    return [(key, names) for key, names in groups.items() if len(names) > 1]


def audit_duplicate_entries(conn: sqlite3.Connection) -> Dict[str, Any]:
    rows = conn.execute(
        """
        SELECT filename, url, raw_json
        FROM files
        ORDER BY filename
        """
    ).fetchall()

    url_dupes = collect_duplicate_groups(rows, "url")

    normalized: List[Dict[str, Any]] = []
    for row in rows:
        file_num = extract_file_num(row["filename"])
        normalized.append({
            "filename": row["filename"],
            "file_num": file_num,
        })

    file_num_groups: Dict[str, List[str]] = {}
    for row in normalized:
        if row["file_num"] is None:
            continue
        key = str(row["file_num"])
        file_num_groups.setdefault(key, []).append(str(row["filename"]))
    file_num_dupes = [(key, names) for key, names in file_num_groups.items() if len(names) > 1]

    return {
        "row_count": len(rows),
        "filename_duplicates_possible": False,
        "filename_reason": "filename is the PRIMARY KEY in SQLite, so exact duplicate filenames cannot exist in this table.",
        "url_duplicates": url_dupes,
        "file_num_duplicates": file_num_dupes,
    }


def audit_disk_index_consistency(conn: sqlite3.Connection, out_dir: str) -> Dict[str, Any]:
    db_rows = conn.execute("SELECT filename, downloaded FROM files ORDER BY filename").fetchall()
    db_names = {str(row["filename"]) for row in db_rows}
    disk_names = {
        name for name in os.listdir(out_dir)
        if os.path.isfile(os.path.join(out_dir, name)) and name.lower().endswith(".pdf")
    }

    orphan_disk = sorted(disk_names - db_names)
    downloaded_missing_disk = sorted(
        str(row["filename"])
        for row in db_rows
        if bool(row["downloaded"]) and str(row["filename"]) not in disk_names
    )
    indexed_not_downloaded_but_on_disk = sorted(
        str(row["filename"])
        for row in db_rows
        if (not bool(row["downloaded"])) and str(row["filename"]) in disk_names
    )

    return {
        "db_rows": len(db_rows),
        "disk_pdfs": len(disk_names),
        "orphan_disk": orphan_disk,
        "downloaded_missing_disk": downloaded_missing_disk,
        "indexed_not_downloaded_but_on_disk": indexed_not_downloaded_but_on_disk,
    }


def duplicate_db_with_downloads_reset(
    source_conn: sqlite3.Connection,
    source_db_path: str,
    target_db_path: str,
) -> Dict[str, int]:
    if os.path.abspath(source_db_path) == os.path.abspath(target_db_path):
        raise RuntimeError("Target DB path must be different from the active DB path.")

    try:
        source_conn.commit()
    except Exception:
        pass

    if os.path.exists(target_db_path):
        os.remove(target_db_path)

    target_conn = sqlite3.connect(target_db_path)
    try:
        source_conn.backup(target_conn)
        target_conn.row_factory = sqlite3.Row

        rows = target_conn.execute(
            """
            SELECT filename, raw_json, downloaded, downloaded_at, bytes, sha256
            FROM files
            ORDER BY filename
            """
        ).fetchall()

        stats = {
            "rows_scanned": len(rows),
            "rows_updated": 0,
            "downloaded_flags_cleared": 0,
            "download_metadata_cleared": 0,
        }

        target_conn.execute(
            """
            UPDATE files
            SET downloaded=0,
                downloaded_at=NULL,
                bytes=NULL,
                sha256=NULL
            """
        )

        raw_json_updates: List[Tuple[str, str]] = []
        for row in rows:
            had_downloaded = bool(row["downloaded"])
            had_metadata = (
                row["downloaded_at"] is not None
                or row["bytes"] is not None
                or row["sha256"] is not None
            )
            if had_downloaded or had_metadata:
                stats["rows_updated"] += 1
            if had_downloaded:
                stats["downloaded_flags_cleared"] += 1
            if had_metadata:
                stats["download_metadata_cleared"] += 1

            try:
                entry = json.loads(row["raw_json"]) if row["raw_json"] else {}
                if not isinstance(entry, dict):
                    entry = {}
            except Exception:
                entry = {}

            entry["downloaded"] = False
            entry["downloaded_at"] = None
            entry["bytes"] = None
            entry["sha256"] = None
            raw_json_updates.append((json.dumps(entry, sort_keys=True), row["filename"]))

        target_conn.executemany(
            "UPDATE files SET raw_json=? WHERE filename=?",
            raw_json_updates,
        )

        target_conn.execute(
            """
            INSERT INTO meta(key, value_json) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json
            """,
            (
                "db_duplicate_reset",
                json.dumps(
                    {
                        "source_db": os.path.abspath(source_db_path),
                        "created_at": iso_now(),
                        "policy": "duplicate_db_and_reset_download_state",
                    },
                    sort_keys=True,
                ),
            ),
        )

        target_conn.commit()
        return stats
    finally:
        target_conn.close()


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
        await ensure_robot_verified(page, dataset_id)
        await ensure_age_verified(page, dataset_id)

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
                "If a captcha/robot check blocks automation, solve it in the browser window."
            )

        await asyncio.sleep(0.5)


async def create_fresh_context(browser, first_page_url: str, dataset_id: int):
    """
    Create an authenticated browser context, validate DOJ access, then close the
    visible page. The context/cookies remain alive and are later copied into a
    Playwright APIRequestContext for page scanning.
    """
    context = await browser.new_context()
    page = await context.new_page()

    log(f"[DS {dataset_id}] NEW CONTEXT - starting DOJ session...")
    await page.goto(first_page_url, wait_until="domcontentloaded", timeout=PAGE_GOTO_TIMEOUT_MS)
    await asyncio.sleep(AUTH_SLEEP_AFTER_GOTO)

    await ensure_robot_verified(page, dataset_id)
    await ensure_age_verified(page, dataset_id)
    await wait_for_dataset_list(page, dataset_id)

    if AUTH_SESSION_SETTLE_SECONDS > 0:
        log(f"[DS {dataset_id}] [auth] Session initialized - settling ({AUTH_SESSION_SETTLE_SECONDS:.1f}s)")
        await asyncio.sleep(AUTH_SESSION_SETTLE_SECONDS)

    if KEEP_AUTH_PAGE_OPEN_SECONDS and KEEP_AUTH_PAGE_OPEN_SECONDS > 0:
        log(f"[DS {dataset_id}] [auth] Holding auth page open ({KEEP_AUTH_PAGE_OPEN_SECONDS:.1f}s)")
        await asyncio.sleep(KEEP_AUTH_PAGE_OPEN_SECONDS)

    if CLOSE_AUTH_PAGE_AFTER_AUTH:
        try:
            await page.close()
            page = None
            log(f"[DS {dataset_id}] [auth] Auth page closed - scan will use request context")
        except Exception:
            pass

    return context, page


async def build_scan_request_context(playwright, browser_context):
    """
    EpRip-style scan worker: copy authenticated browser cookies/storage into a
    lightweight APIRequestContext so scanning does not need a browser page open.
    """
    storage_state = await browser_context.storage_state()
    return await playwright.request.new_context(
        storage_state=storage_state,
        user_agent=(
            "Mozilla/5.0 (X11; Linux x86_64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/143.0.0.0 Safari/537.36"
        ),
    )


async def dispose_request_context(request_context) -> None:
    if request_context is None:
        return
    try:
        await request_context.dispose()
    except Exception:
        pass


async def fetch_scan_page_html(request_context, url: str, referer: str) -> Tuple[int, str]:
    resp = await request_context.get(
        url,
        timeout=180000,
        headers={
            "Referer": referer,
            "Accept": "text/html,application/xhtml+xml,*/*",
        },
    )
    return resp.status, await resp.text()


async def fetch_scan_page_html_with_retry(
    request_context,
    dataset_id: int,
    page_num: int,
    url: str,
    referer: str,
) -> Tuple[int, str]:
    last_err: Optional[Exception] = None
    last_status: Optional[int] = None

    for attempt in range(1, MAX_SCAN_PAGE_FETCH_RETRIES + 1):
        try:
            status, html = await fetch_scan_page_html(request_context, url, referer)
            last_status = status
            if status == 429 or 500 <= status <= 599:
                if attempt >= MAX_SCAN_PAGE_FETCH_RETRIES:
                    return status, html
                delay = backoff_sleep_seconds(attempt)
                log(
                    f"[DS {dataset_id}] [scan] Transient HTTP {status} on page {page_num} "
                    f"(attempt {attempt}/{MAX_SCAN_PAGE_FETCH_RETRIES}) -> backoff {delay:.2f}s"
                )
                await asyncio.sleep(delay)
                continue
            return status, html
        except Exception as e:
            last_err = e
            msg = str(e)
            retryable = is_retryable_playwright_error(msg)
            if not retryable or attempt >= MAX_SCAN_PAGE_FETCH_RETRIES:
                raise

            delay = backoff_sleep_seconds(attempt)
            log(
                f"[DS {dataset_id}] [scan] Retryable page fetch error on page {page_num} "
                f"(attempt {attempt}/{MAX_SCAN_PAGE_FETCH_RETRIES}) -> backoff {delay:.2f}s"
            )
            await asyncio.sleep(delay)

    if last_status is not None:
        return last_status, ""
    raise RuntimeError(f"[DS {dataset_id}] [scan] Exhausted page fetch retries for page {page_num}: {repr(last_err)}")


def html_has_scan_auth_gate(html: str) -> bool:
    if not html:
        return False
    lowered = html.lower().replace("\\/", "/")
    if "block-usdoj-external-files-block" in lowered or "/epstein/files/" in lowered:
        return False
    return (
        "access denied" in lowered
        or "403 forbidden" in lowered
        or "forbidden" in lowered
        or "i am not a robot" in lowered
        or "age-button-yes" in lowered
        or "age-verify-block" in lowered
    )


def extract_scan_page_pdfs(html: str) -> List[Tuple[str, str]]:
    """
    Extract PDF URLs from raw HTML. This mirrors EpRip's request-context scanner
    and does not require DOM/eval/page navigation.
    """
    pdfs: List[Tuple[str, str]] = []
    if not html:
        return pdfs

    normalized_html = html.replace("\\/", "/")
    seen = set()
    matches = re.findall(
        r'(/epstein/files/[^"\'\s>]+?\.pdf|https?://[^"\'\s>]+?/epstein/files/[^"\'\s>]+?\.pdf)',
        normalized_html,
        flags=re.IGNORECASE,
    )
    for href in matches:
        full_url = urljoin(BASE_SITE, href.strip())
        if is_valid_epstein_pdf_url(full_url):
            filename = os.path.basename(urlparse(full_url).path)
            if filename and filename not in seen:
                seen.add(filename)
                pdfs.append((filename, full_url))

    return pdfs


async def refresh_scan_auth_in_context(
    context,
    first_page_url: str,
    dataset_id: int,
) -> None:
    """
    Re-open a visible page only for re-auth, then close it again. The caller
    should rebuild the APIRequestContext afterward so it gets fresh storage state.
    """
    page = await context.new_page()
    try:
        log(f"[DS {dataset_id}] [scan] Refreshing auth window...")
        await page.goto(first_page_url, wait_until="domcontentloaded", timeout=PAGE_GOTO_TIMEOUT_MS)
        await asyncio.sleep(AUTH_SLEEP_AFTER_GOTO)
        await ensure_robot_verified(page, dataset_id=dataset_id)
        await ensure_age_verified(page, dataset_id=dataset_id)
        await wait_for_dataset_list(page, dataset_id=dataset_id, timeout_s=AUTH_WAIT_SECONDS)
        if AUTH_SESSION_SETTLE_SECONDS and AUTH_SESSION_SETTLE_SECONDS > 0:
            await asyncio.sleep(AUTH_SESSION_SETTLE_SECONDS)
    finally:
        try:
            await page.close()
        except Exception:
            pass


async def scrape_page_for_pdfs(
    playwright,
    browser_context,
    request_context,
    page_url: str,
    dataset_id: int,
    page_num: int,
    first_page_url: str,
) -> Tuple[List[Tuple[str, str]], Optional[str], Any]:
    """
    Request-context page scan. Returns (pdfs, error, request_context).
    If auth appears stale, it opens a temporary visible auth page, refreshes the
    browser context, rebuilds the request context, and retries the same page.
    """
    auth_refresh_attempts = 0
    referer = first_page_url

    while True:
        try:
            status, html = await fetch_scan_page_html_with_retry(
                request_context,
                dataset_id=dataset_id,
                page_num=page_num,
                url=page_url,
                referer=referer,
            )

            if status >= 400:
                if status in {401, 403} and auth_refresh_attempts < MAX_SCAN_AUTH_REFRESH_RETRIES:
                    log(
                        f"[DS {dataset_id}] [scan] HTTP {status} on page {page_num}; refreshing auth "
                        f"(attempt {auth_refresh_attempts + 1}/{MAX_SCAN_AUTH_REFRESH_RETRIES})"
                    )
                    await refresh_scan_auth_in_context(browser_context, first_page_url, dataset_id)
                    await dispose_request_context(request_context)
                    request_context = await build_scan_request_context(playwright, browser_context)
                    auth_refresh_attempts += 1
                    await asyncio.sleep(SCAN_AUTH_REFRESH_COOLDOWN)
                    continue
                return [], f"HTTP {status}", request_context

            if html_has_scan_auth_gate(html):
                if auth_refresh_attempts < MAX_SCAN_AUTH_REFRESH_RETRIES:
                    log(
                        f"[DS {dataset_id}] [scan] Auth/gate HTML detected on page {page_num}; refreshing auth "
                        f"(attempt {auth_refresh_attempts + 1}/{MAX_SCAN_AUTH_REFRESH_RETRIES})"
                    )
                    await refresh_scan_auth_in_context(browser_context, first_page_url, dataset_id)
                    await dispose_request_context(request_context)
                    request_context = await build_scan_request_context(playwright, browser_context)
                    auth_refresh_attempts += 1
                    await asyncio.sleep(SCAN_AUTH_REFRESH_COOLDOWN)
                    continue
                return [], "AUTH_GATE_AFTER_REFRESH_LIMIT", request_context

            return extract_scan_page_pdfs(html), None, request_context

        except Exception as e:
            msg = repr(e)
            if auth_refresh_attempts < MAX_SCAN_AUTH_REFRESH_RETRIES:
                log(
                    f"[DS {dataset_id}] [scan] PAGE FETCH FAILED on page {page_num}: {msg}; refreshing auth "
                    f"(attempt {auth_refresh_attempts + 1}/{MAX_SCAN_AUTH_REFRESH_RETRIES})"
                )
                try:
                    await refresh_scan_auth_in_context(browser_context, first_page_url, dataset_id)
                    await dispose_request_context(request_context)
                    request_context = await build_scan_request_context(playwright, browser_context)
                    auth_refresh_attempts += 1
                    await asyncio.sleep(SCAN_AUTH_REFRESH_COOLDOWN)
                    continue
                except Exception as refresh_err:
                    return [], f"{msg}; auth refresh failed: {repr(refresh_err)}", request_context
            return [], msg, request_context


def fingerprint_filenames(names: List[str]) -> str:
    s = "\n".join(sorted(names))
    return hashlib.sha1(s.encode("utf-8")).hexdigest()


async def scan_pages(
    playwright,
    browser_context,
    request_context,
    conn: sqlite3.Connection,
    dataset_id: int,
    base_url: str,
    out_dir: str,
    hydrate_from_disk: bool,
    start_page: int,
    use_no_new_streak: bool,
    no_new_limit: int,
    no_new_streak_start: int = 0,
    stop_at_page: Optional[int] = None,
) -> Tuple[int, int, Any]:
    mode = "DISCOVERY" if use_no_new_streak else "REWALK"
    log(
        f"[DS {dataset_id}] {mode} scan start at page {start_page} "
        f"(streak={no_new_streak_start}, stop_after_no_new={no_new_limit})"
    )

    pages_no_new = int(no_new_streak_start)
    page_num = int(start_page)
    last_scanned = page_num - 1
    hard_failures = 0
    first_page_url = base_url.format(1)

    while True:
        if page_num > MAX_PAGES_HARD_CAP:
            log(f"[DS {dataset_id}] HARD CAP reached at page {page_num}. Stopping.")
            conn.commit()
            break

        page_url = base_url.format(page_num)
        log(f"[DS {dataset_id}] Scanning page {page_num}")

        pdfs, err, request_context = await scrape_page_for_pdfs(playwright, browser_context, request_context, page_url, dataset_id, page_num, first_page_url)

        pdf_found_total = len(pdfs)
        efta_found = 0
        new_this_page = 0
        status = "ok"
        error_text = None
        page_hash: Optional[str] = None
        efta_names: List[str] = []

        if err is not None:
            hard_failures += 1
            status = "error"
            error_text = err
            log(f"[DS {dataset_id}] ERROR scanning page {page_num}: {err}")

            if hard_failures >= MAX_SCAN_PAGE_HARD_FAILURES:
                log(
                    f"[DS {dataset_id}] Too many scan failures ({hard_failures}). "
                    f"Cooling down {SCAN_PAGE_HARD_FAILURE_COOLDOWN:.1f}s and stopping this scan."
                )
                await asyncio.sleep(SCAN_PAGE_HARD_FAILURE_COOLDOWN)
                upsert_page_result(conn, dataset_id, page_num, status, 0, 0, 0, None, error_text)
                set_resume_state(conn, dataset_id, page_num, pages_no_new if use_no_new_streak else 0, page_num)
                conn.commit()
                break
        else:
            hard_failures = 0

            for filename, full_url in pdfs:
                file_num = extract_file_num(filename)
                if file_num is None:
                    continue
                efta_found += 1
                efta_names.append(filename)
                if upsert_file(conn, dataset_id, filename, file_num, full_url, page_num, out_dir, hydrate_from_disk):
                    new_this_page += 1

            page_hash = fingerprint_filenames(efta_names)

            log(f"[DS {dataset_id}] Found {pdf_found_total} PDFs ({efta_found} EFTA) on page {page_num}")

            if use_no_new_streak:
                if new_this_page == 0:
                    pages_no_new += 1
                    if efta_found > 0:
                        log(
                            f"[DS {dataset_id}] Page {page_num} added no first-time entries; "
                            f"{efta_found} EFTA PDFs were already seen earlier "
                            f"(streak={pages_no_new}/{no_new_limit})"
                        )
                    else:
                        log(
                            f"[DS {dataset_id}] No EFTA PDFs found on page {page_num} "
                            f"(streak={pages_no_new}/{no_new_limit})"
                        )
                else:
                    pages_no_new = 0
                    log(f"[DS {dataset_id}] NEW PDFs discovered on page {page_num}: {new_this_page}")
            else:
                if new_this_page == 0:
                    if efta_found > 0:
                        log(
                            f"[DS {dataset_id}] Page {page_num} added no first-time entries; "
                            f"{efta_found} EFTA PDFs were already seen earlier "
                            "(rewalk mode; streak ignored)"
                        )
                    else:
                        log(f"[DS {dataset_id}] No EFTA PDFs found on page {page_num} (rewalk mode; streak ignored)")
                else:
                    log(f"[DS {dataset_id}] NEW PDFs discovered on page {page_num}: {new_this_page}")

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

        set_resume_state(
            conn,
            dataset_id=dataset_id,
            next_page=page_num + 1,
            no_new_streak=(pages_no_new if use_no_new_streak else 0),
            last_scan_page=page_num,
        )

        conn.commit()
        last_scanned = page_num

        if stop_at_page is not None and page_num >= stop_at_page:
            log(f"[DS {dataset_id}] Stop-at-page reached: {page_num} (target={stop_at_page}).")
            break

        if use_no_new_streak and pages_no_new >= no_new_limit:
            log(f"[DS {dataset_id}] Stopping discovery: no NEW entries for {no_new_limit} consecutive pages.")
            break

        page_num += 1
        await asyncio.sleep(SLEEP_BETWEEN_PAGES)

    return last_scanned, pages_no_new, request_context


async def repair_pages_only(
    playwright,
    browser_context,
    request_context,
    conn: sqlite3.Connection,
    dataset_id: int,
    base_url: str,
    out_dir: str,
    hydrate_from_disk: bool,
    pages_to_repair: List[int],
) -> Any:
    if not pages_to_repair:
        log(f"[DS {dataset_id}] No suspect/error pages to repair.")
        return

    log(f"[DS {dataset_id}] Repair mode - pages to repair: {len(pages_to_repair)}")
    first_page_url = base_url.format(1)

    for page_num in pages_to_repair:
        page_url = base_url.format(page_num)
        log(f"[DS {dataset_id}] [REPAIR] Scanning page {page_num}")

        pdfs, err, request_context = await scrape_page_for_pdfs(playwright, browser_context, request_context, page_url, dataset_id, page_num, first_page_url)

        pdf_found_total = len(pdfs)
        efta_found = 0
        new_this_page = 0
        status = "ok"
        error_text = None
        efta_names: List[str] = []

        if err is not None:
            status = "error"
            error_text = err
            page_hash = fingerprint_filenames([])
            log(f"[DS {dataset_id}] [REPAIR] ERROR page {page_num}: {err}")
        else:
            for filename, full_url in pdfs:
                file_num = extract_file_num(filename)
                if file_num is None:
                    continue
                efta_found += 1
                efta_names.append(filename)
                if upsert_file(conn, dataset_id, filename, file_num, full_url, page_num, out_dir, hydrate_from_disk):
                    new_this_page += 1

            page_hash = fingerprint_filenames(efta_names)
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
        set_resume_state(conn, dataset_id, max(page_num + 1, get_resume_state(conn, dataset_id)[0]), 0, page_num)
        conn.commit()
        await asyncio.sleep(SLEEP_BETWEEN_PAGES)

    log(f"[DS {dataset_id}] Repair pass complete.")
    return request_context


async def rewalk_then_discover(
    playwright,
    browser_context,
    request_context,
    conn: sqlite3.Connection,
    dataset_id: int,
    base_url: str,
    out_dir: str,
    hydrate_from_disk: bool,
    start_page: int,
    no_new_limit: int,
    rewalk_end: Optional[int] = None,
) -> Any:
    frontier = get_frontier_page(conn, dataset_id)
    end_page = rewalk_end if (rewalk_end is not None and rewalk_end >= 1) else frontier

    if end_page < 1:
        log(f"[DS {dataset_id}] No frontier yet - skipping rewalk; starting discovery at page {start_page}")
        last_scanned, streak, request_context = await scan_pages(
            playwright,
            browser_context,
            request_context,
            conn,
            dataset_id,
            base_url,
            out_dir,
            hydrate_from_disk,
            start_page,
            True,
            no_new_limit,
            0,
            None,
        )
        return request_context

    log(f"[DS {dataset_id}] Two-phase run: REWALK {start_page} -> {end_page}, then DISCOVERY from {max(start_page, end_page + 1)}")

    if end_page >= start_page:
        last_scanned, streak, request_context = await scan_pages(
            playwright,
            browser_context,
            request_context,
            conn,
            dataset_id,
            base_url,
            out_dir,
            hydrate_from_disk,
            start_page,
            False,
            no_new_limit,
            0,
            end_page,
        )

    discover_start = max(start_page, end_page + 1)
    last_scanned, streak, request_context = await scan_pages(
        playwright,
        browser_context,
        request_context,
        conn,
        dataset_id,
        base_url,
        out_dir,
        hydrate_from_disk,
        discover_start,
        True,
        no_new_limit,
        0,
        None,
    )
    return request_context


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


def ui_pick_action() -> str:
    print("\nScan options:")
    print("  1) Resume DISCOVERY scan (continue from resume next_page; uses no-new streak)")
    print("  2) Full REWALK -> frontier, then continue DISCOVERY (single run) [recommended]")
    print("  3) Repair suspect/error pages only (0 PDFs / repeat / errors)")
    print("  4) Rewalk custom range, then continue DISCOVERY (single run)")
    print("  5) Index utility work")
    print("  6) Show DB stats")
    print("  7) Exit")

    while True:
        raw = input("Choose [1]: ").strip() or "1"
        if raw in {"1", "2", "3", "4", "5", "6", "7"}:
            return raw
        print("Invalid option.")


def ui_pick_utility_action() -> str:
    print("\nIndex utilities:")
    print("  1) Repair DB from disk: mark on-disk PDFs as downloaded")
    print("  2) Audit duplicate-like entries")
    print("  3) Disk/DB consistency report")
    print("  4) Duplicate active DB and reset download state")
    print("  5) Back")

    while True:
        raw = input("Choose [1]: ").strip() or "1"
        if raw in {"1", "2", "3", "4", "5"}:
            return raw
        print("Invalid option.")


def print_duplicate_audit(audit: Dict[str, Any]) -> None:
    print("\n=== DUPLICATE AUDIT ===")
    print(f"Rows scanned:             {audit['row_count']}")
    print(f"Filename duplicates:      impossible")
    print(f"Reason:                   {audit['filename_reason']}")
    print(f"Duplicate URLs:           {len(audit['url_duplicates'])}")
    print(f"Duplicate file numbers:   {len(audit['file_num_duplicates'])}")

    if audit["url_duplicates"]:
        print("\nSample duplicate URLs:")
        for url, filenames in audit["url_duplicates"][:10]:
            print(f"  {url}")
            print(f"    -> {', '.join(filenames[:5])}{' ...' if len(filenames) > 5 else ''}")

    if audit["file_num_duplicates"]:
        print("\nSample duplicate file numbers:")
        for file_num, filenames in audit["file_num_duplicates"][:10]:
            print(f"  {file_num}")
            print(f"    -> {', '.join(filenames[:5])}{' ...' if len(filenames) > 5 else ''}")

    print("=======================\n")


def print_disk_consistency_report(report: Dict[str, Any]) -> None:
    print("\n=== DISK / DB REPORT ===")
    print(f"DB rows:                          {report['db_rows']}")
    print(f"PDFs on disk:                     {report['disk_pdfs']}")
    print(f"On disk, not in DB:               {len(report['orphan_disk'])}")
    print(f"Marked downloaded, missing disk:  {len(report['downloaded_missing_disk'])}")
    print(f"On disk, not marked downloaded:   {len(report['indexed_not_downloaded_but_on_disk'])}")

    if report["orphan_disk"]:
        print("\nSample on-disk PDFs not in DB:")
        print("  " + ", ".join(report["orphan_disk"][:15]))

    if report["downloaded_missing_disk"]:
        print("\nSample DB rows marked downloaded but missing on disk:")
        print("  " + ", ".join(report["downloaded_missing_disk"][:15]))

    if report["indexed_not_downloaded_but_on_disk"]:
        print("\nSample PDFs on disk but not marked downloaded:")
        print("  " + ", ".join(report["indexed_not_downloaded_but_on_disk"][:15]))

    print("========================\n")


def ui_int(prompt: str, default: Optional[int] = None, min_v: Optional[int] = None) -> Optional[int]:
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


def ui_text(prompt: str, default: Optional[str] = None) -> str:
    suffix = f" [{default}]" if default else ""
    raw = input(f"{prompt}{suffix}: ").strip()
    return raw or (default or "")


def ui_yes_no(prompt: str, default: bool = False) -> bool:
    suffix = " [Y/n]" if default else " [y/N]"
    raw = input(f"{prompt}{suffix}: ").strip().lower()
    if not raw:
        return default
    return raw in {"y", "yes"}


def ui_pick_no_new_limit(default: int = MAX_PAGES_WITH_NO_NEW_PDFS) -> int:
    while True:
        raw = input(f"No-new streak stop threshold [{default}]: ").strip()
        if raw == "":
            return default
        try:
            value = int(raw)
            if value < 1:
                print("Value must be >= 1")
                continue
            return value
        except ValueError:
            print("Enter an integer or press ENTER.")


def ui_pick_hydrate_from_disk(default: bool = True) -> bool:
    return ui_yes_no("Add on-disk PDFs to this database?", default=default)


def ui_pick_index_source(dataset_id: int, out_dir: str, default_db_path: str) -> Tuple[str, str]:
    found = discover_index_files(dataset_id, out_dir)
    pdf_count = count_pdf_files(out_dir)

    print("\nIndex files in dataset directory:")
    print(f"  PDFs already on disk: {pdf_count}")
    print(f"  SQLite indexes found: {len(found['sqlite'])}")
    print(f"  JSON indexes found:   {len(found['json'])}")

    options: List[Tuple[str, str, Optional[str]]] = []
    for path in found["sqlite"]:
        options.append(("sqlite", f"Use existing SQLite: {os.path.basename(path)}", path))
    for path in found["json"]:
        options.append(("json", f"Import JSON into SQLite: {os.path.basename(path)}", path))

    new_default = default_db_path
    if os.path.exists(new_default):
        new_default = unique_path_variant(default_db_path, "_new")
    options.append(("new", f"Start NEW SQLite index ({os.path.basename(new_default)})", new_default))

    print("")
    for idx, (_kind, label, _path) in enumerate(options, start=1):
        print(f"  {idx}) {label}")

    while True:
        raw = input("Choose index source [1]: ").strip() or "1"
        try:
            chosen = int(raw)
        except ValueError:
            chosen = 0
        if 1 <= chosen <= len(options):
            break
        print("Invalid option.")

    kind, _label, selected_path = options[chosen - 1]
    if kind == "sqlite":
        assert selected_path is not None
        return kind, selected_path

    if kind == "json":
        assert selected_path is not None
        return kind, selected_path

    assert selected_path is not None
    new_db_path = ui_text("New SQLite index path", selected_path)
    if os.path.exists(new_db_path):
        if not ui_yes_no(f"Overwrite existing file?\n  {new_db_path}", default=False):
            raise SystemExit("Canceled.")
        os.remove(new_db_path)
    return kind, new_db_path


# ------------------ Main ------------------

async def run_dataset(dataset_id: int, headless: bool, no_new_limit: Optional[int]) -> None:
    cfg = DATASETS[dataset_id]
    out_dir = cfg["out_dir"]
    os.makedirs(out_dir, exist_ok=True)

    default_db_path = db_path_for_dataset(out_dir, cfg["db_file"])
    source_kind, source_path = ui_pick_index_source(dataset_id, out_dir, default_db_path)
    hydrate_from_disk = ui_pick_hydrate_from_disk(default=True)
    db_path = source_path

    if source_kind == "json":
        default_import_db = unique_path_variant(
            os.path.splitext(source_path)[0] + ".sqlite",
            "_import",
        )
        db_path = ui_text("SQLite working DB to merge JSON into", default_import_db)
        import_stats = import_json_index_into_sqlite(
            source_path,
            db_path,
            dataset_id,
            out_dir,
            hydrate_from_disk,
        )
        log(
            f"[DS {dataset_id}] JSON import merge complete: "
            f"{os.path.basename(source_path)} -> {os.path.basename(db_path)} "
            f"(added={import_stats['rows_added']}, existing_skipped={import_stats['rows_skipped_existing']}, "
            f"invalid_skipped={import_stats['rows_skipped_invalid']})"
        )
        print("\n=== JSON IMPORT MERGE ===")
        print(f"Source JSON:             {source_path}")
        print(f"Target SQLite:           {db_path}")
        print(f"Created new DB:          {'yes' if import_stats['created_new_db'] else 'no'}")
        print(f"JSON entries seen:       {import_stats['json_entries_seen']}")
        print(f"Rows added:              {import_stats['rows_added']}")
        print(f"Existing rows skipped:   {import_stats['rows_skipped_existing']}")
        print(f"Invalid rows skipped:    {import_stats['rows_skipped_invalid']}")
        print("Merge policy:            add missing only by filename")
        print("=========================\n")

    conn = connect_db(db_path)
    init_db(conn, dataset_id, out_dir, db_path)
    meta_upsert(conn, "hydrate_from_disk", bool(hydrate_from_disk))
    conn.commit()

    if hydrate_from_disk:
        startup_sync_stats = sync_db_from_disk(conn, dataset_id, out_dir)
        print_startup_disk_sync_summary(startup_sync_stats, db_path)

    initial_files_count = conn.execute("SELECT COUNT(*) AS c FROM files").fetchone()["c"]

    base_url = cfg["base_url"]
    log(f"[DS {dataset_id}] Working SQLite index: {db_path}")
    log(f"[DS {dataset_id}] Add on-disk PDFs to database: {'yes' if hydrate_from_disk else 'no'}")

    async with async_playwright() as p:
        browser = None
        context = None
        page = None
        request_context = None

        async def ensure_scan_runtime() -> None:
            nonlocal browser, context, page, request_context
            if browser is None:
                browser = await p.chromium.launch(headless=headless, slow_mo=25)
            if context is None:
                context, page = await create_fresh_context(browser, base_url.format(1), dataset_id)
                request_context = await build_scan_request_context(p, context)
                log(f"[DS {dataset_id}] [scan] Request context ready - browser page is not required for scanning")

        try:
            while True:
                action = ui_pick_action()

                if action == "7":
                    break

                if action == "6":
                    print_stats(conn, dataset_id)
                    continue

                if action == "5":
                    while True:
                        utility_action = ui_pick_utility_action()
                        if utility_action == "5":
                            break

                        if utility_action == "1":
                            repair_stats = repair_downloaded_flags_from_disk(conn, out_dir)
                            print("\n=== DISK REPAIR ===")
                            print(f"Rows scanned:         {repair_stats['rows_scanned']}")
                            print(f"Rows updated:         {repair_stats['rows_updated']}")
                            print(f"Marked downloaded:    {repair_stats['marked_downloaded']}")
                            print(f"Flipped to false:     {repair_stats['flipped_to_false']}")
                            print(f"Byte count updates:   {repair_stats['bytes_updated']}")
                            print(f"Already downloaded:   {repair_stats['already_downloaded']}")
                            print(f"Missing on disk:      {repair_stats['missing_on_disk']}")
                            print(f"Invalid on disk:      {repair_stats['invalid_on_disk']}")
                            print("===================\n")
                            continue

                        if utility_action == "2":
                            print_duplicate_audit(audit_duplicate_entries(conn))
                            continue

                        if utility_action == "3":
                            print_disk_consistency_report(audit_disk_index_consistency(conn, out_dir))
                            continue

                        if utility_action == "4":
                            default_clone_path = unique_path_variant(db_path, "_clean")
                            clone_path = ui_text("Duplicate DB path", default_clone_path)
                            if os.path.abspath(clone_path) == os.path.abspath(db_path):
                                print("Target path must be different from the active DB.")
                                continue
                            if os.path.exists(clone_path):
                                if not ui_yes_no(f"Overwrite existing file?\n  {clone_path}", default=False):
                                    print("Canceled.")
                                    continue

                            clone_stats = duplicate_db_with_downloads_reset(conn, db_path, clone_path)
                            print("\n=== DB DUPLICATE ===")
                            print(f"Source DB:                 {db_path}")
                            print(f"Duplicate DB:              {clone_path}")
                            print("Original DB modified:      no")
                            print(f"Rows scanned:              {clone_stats['rows_scanned']}")
                            print(f"Rows updated in clone:     {clone_stats['rows_updated']}")
                            print(f"Downloaded flags cleared:  {clone_stats['downloaded_flags_cleared']}")
                            print(f"Download metadata cleared: {clone_stats['download_metadata_cleared']}")
                            print("====================\n")
                            continue

                    continue

                current_no_new_limit = no_new_limit if (no_new_limit is not None and no_new_limit >= 1) else ui_pick_no_new_limit()
                log(
                    f"[DS {dataset_id}] Discovery stop threshold: "
                    f"{current_no_new_limit} consecutive pages with no new entries"
                )

                await ensure_scan_runtime()

                if action == "1":
                    next_page, streak, _last = get_resume_state(conn, dataset_id)
                    _last_scanned, _streak, request_context = await scan_pages(
                        playwright=p,
                        browser_context=context,
                        request_context=request_context,
                        conn=conn,
                        dataset_id=dataset_id,
                        base_url=base_url,
                        out_dir=out_dir,
                        hydrate_from_disk=hydrate_from_disk,
                        start_page=next_page,
                        use_no_new_streak=True,
                        no_new_limit=current_no_new_limit,
                        no_new_streak_start=streak,
                    )
                    continue

                if action == "2":
                    frontier = get_frontier_page(conn, dataset_id)
                    start = ui_int("Start page", default=1, min_v=1) or 1
                    log(f"[DS {dataset_id}] Full REWALK->frontier ({frontier}) then DISCOVERY starting at {max(start, frontier + 1)}")
                    request_context = await rewalk_then_discover(
                        playwright=p,
                        browser_context=context,
                        request_context=request_context,
                        conn=conn,
                        dataset_id=dataset_id,
                        base_url=base_url,
                        out_dir=out_dir,
                        hydrate_from_disk=hydrate_from_disk,
                        start_page=start,
                        no_new_limit=current_no_new_limit,
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

                    request_context = await repair_pages_only(
                        playwright=p,
                        browser_context=context,
                        request_context=request_context,
                        conn=conn,
                        dataset_id=dataset_id,
                        base_url=base_url,
                        out_dir=out_dir,
                        hydrate_from_disk=hydrate_from_disk,
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

                    request_context = await rewalk_then_discover(
                        playwright=p,
                        browser_context=context,
                        request_context=request_context,
                        conn=conn,
                        dataset_id=dataset_id,
                        base_url=base_url,
                        out_dir=out_dir,
                        hydrate_from_disk=hydrate_from_disk,
                        start_page=start,
                        no_new_limit=current_no_new_limit,
                        rewalk_end=end,
                    )
                    continue

        except (KeyboardInterrupt, asyncio.CancelledError):
            clear_current_task_cancellation()
            log(f"[DS {dataset_id}] Ctrl+C received - finalizing shutdown")
            print_interrupt_summary(conn, dataset_id, db_path, int(initial_files_count))
            return

        finally:
            try:
                if request_context is not None:
                    await dispose_request_context(request_context)
            except Exception:
                pass
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
    p = argparse.ArgumentParser(description="EpRip-compatible DOJ Epstein SQLite index scanner.")
    p.add_argument("--headless", action="store_true", help="Headless browser. Not recommended if robot/captcha appears.")
    p.add_argument("--dataset", type=int, default=0, help="Dataset number 1-12. If omitted, UI prompt is used.")
    p.add_argument(
        "--no-new-limit",
        type=int,
        default=0,
        help=f"Stop discovery after this many consecutive pages with no new entries (default: {MAX_PAGES_WITH_NO_NEW_PDFS}).",
    )
    return p.parse_args()


async def main() -> None:
    args = parse_args()
    dataset_id = args.dataset if args.dataset in DATASETS else ui_pick_dataset()
    no_new_limit = args.no_new_limit if args.no_new_limit >= 1 else None
    log(f"Selected dataset: {dataset_id}")
    await run_dataset(dataset_id, headless=args.headless, no_new_limit=no_new_limit)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
