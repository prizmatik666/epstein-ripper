#!/usr/bin/env python3
import csv
import math
import os
import shutil
import sqlite3
import sys
import time
import traceback
from pathlib import Path
from datetime import datetime, timedelta
from multiprocessing import Pool, cpu_count

# sorts folder containing image (like pulled from pdf's
# using image_ripper . And moves all black(redatcted)
# images, images that appear to be all text/documents, and
# other images that have traits that dont seem like an
# image/picture. It puts these in different categories/buckets
# to go through manually and review. I suggest using (M)ove
# instead of (C)opy , to avoid massive memory ballooning on hard disk
# that would happen from copying a massive ammount of files
# on a harddisk

try:
    import numpy as np
    from PIL import Image, ImageFile, UnidentifiedImageError
except ImportError:
    print("Missing required packages.")
    print("Install with: pip install pillow numpy")
    sys.exit(1)

ImageFile.LOAD_TRUNCATED_IMAGES = True


# ============================================================
# CONFIG
# ============================================================

SUPPORTED_EXTS = {
    ".jpg", ".jpeg", ".png", ".bmp", ".gif", ".tif", ".tiff", ".webp"
}

QUARANTINE_NAME = "_quarantine"
LOG_NAME = "_quarantine_log.txt"
CACHE_DB_NAME = "_scan_cache.sqlite3"
CSV_REPORT_NAME = "_quarantine_report.csv"

BUCKET_BLACK = "black_pages"
BUCKET_DOCS = "docs"
BUCKET_REVIEW_HIGH = "review_high"
BUCKET_REVIEW_LOW = "review_low"

ANALYZE_MAX_DIM = 256
BLACK_PIXEL_THRESHOLD = 28
WHITE_PIXEL_THRESHOLD = 227
MOSTLY_BLACK_RATIO = 0.96
MOSTLY_WHITE_RATIO = 0.75
LOW_COLORFULNESS_STD = 14.0
QUARANTINE_SCORE_THRESHOLD = 3.0
REVIEW_HIGH_SCORE_THRESHOLD = 4.25
MIN_TEXT_ROWS_RATIO = 0.06
MAX_TEXT_ROWS_RATIO = 0.65
TINY_IMAGE_IGNORE_DIM = 48

COPY_WARNING_VERBATIM = (
    "WARNING: running copy mode on large directories can cause storage drives "
    "to have a catastrophic existential crisis."
)

DEFAULT_MIN_FREE_GB_MOVE = 10.0
DEFAULT_MIN_FREE_GB_COPY = 25.0
DEFAULT_MIN_FREE_GB_DRY = 5.0

PROGRESS_BAR_WIDTH = 30
CHUNKSIZE = 100
AUTO_WORKER_CAP = 4
CACHE_FLUSH_EVERY_N = 250
CACHE_FLUSH_EVERY_SECONDS = 5.0
CHECKPOINT_EVERY_FILES = 2000
CHECKPOINT_EVERY_HITS = 100

RUN_STATUS_RUNNING = "running"
RUN_STATUS_COMPLETED = "completed"
RUN_STATUS_INTERRUPTED = "interrupted"
RUN_STATUS_FAILED = "failed"
RUN_STATUS_STOPPED_GUARD = "stopped_guard"


# ============================================================
# FORMAT HELPERS
# ============================================================

def format_bytes(num_bytes: int) -> str:
    units = ["B", "KB", "MB", "GB", "TB", "PB"]
    value = float(max(0, num_bytes))
    for unit in units:
        if value < 1024.0 or unit == units[-1]:
            if unit in {"B", "KB"}:
                return f"{value:.0f} {unit}"
            return f"{value:.2f} {unit}"
        value /= 1024.0
    return f"{num_bytes} B"


def human_folder_size_display(num_bytes: int) -> str:
    mb = num_bytes / (1024 ** 2)
    gb = num_bytes / (1024 ** 3)
    if gb >= 1:
        return f"{mb:,.2f} MB ({gb:,.2f} GB)"
    return f"{mb:,.2f} MB"


def format_seconds(seconds: float) -> str:
    if seconds < 0 or math.isinf(seconds) or math.isnan(seconds):
        return "--:--:--"
    td = timedelta(seconds=int(seconds))
    total_seconds = int(td.total_seconds())
    hours = total_seconds // 3600
    minutes = (total_seconds % 3600) // 60
    secs = total_seconds % 60
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def format_rate(n: int, elapsed: float) -> str:
    if elapsed <= 0:
        return "0.00/s"
    return f"{n / elapsed:.2f}/s"


def ratio_str(part: int, whole: int) -> str:
    if whole <= 0:
        return "0.00%"
    return f"{(part / whole) * 100.0:.2f}%"


# ============================================================
# SAFE IO / LOGGING
# ============================================================

def safe_print(msg: str = "", end: str = "\n", flush: bool = True):
    try:
        print(msg, end=end, flush=flush)
    except Exception:
        pass


def safe_log_append(log_path: Path, text: str):
    try:
        with open(log_path, "a", encoding="utf-8", errors="replace") as f:
            f.write(text)
    except Exception as e:
        safe_print(f"\n[WARN] Failed to write log file: {e}")


def write_log_header(
    log_path: Path,
    scan_root: Path,
    scan_mode: str,
    action_mode: str,
    workers: int,
    min_free_gb: float,
    scan_dir_size_bytes: int,
    initial_free_bytes: int,
    bucket_config: dict,
):
    text = (
        "\n" + "=" * 80 + "\n"
        f"Run started:        {datetime.now().isoformat(timespec='seconds')}\n"
        f"Scan root:          {scan_root}\n"
        f"Scan mode:          {scan_mode}\n"
        f"Action mode:        {action_mode}\n"
        f"Workers:            {workers}\n"
        f"Min free space GB:  {min_free_gb}\n"
        f"Scan dir size:      {scan_dir_size_bytes} bytes\n"
        f"Initial free space: {initial_free_bytes} bytes\n"
        f"Bucket config:      {bucket_config}\n"
        + "=" * 80 + "\n"
    )
    safe_log_append(log_path, text)


def log_action(log_path: Path, src: Path, intended_dst: Path, final_dst: Path, result: dict, action_mode: str):
    metrics = result.get("metrics", {})
    text = (
        "\nACTION:\n"
        f"  MODE:          {action_mode.upper()}\n"
        f"  BUCKET:        {result.get('bucket')}\n"
        f"  FROM:          {src}\n"
        f"  INTENDED TO:   {intended_dst}\n"
        f"  FINAL TO:      {final_dst}\n"
        f"  SCORE:         {result.get('score', 0.0):.2f}\n"
        f"  STRENGTH:      {result.get('confidence', 'n/a')}\n"
        f"  REASONS:       {' | '.join(result.get('reasons', []))}\n"
        "  METRICS: "
        f"black={metrics.get('black_ratio', 0.0):.3f}, "
        f"white={metrics.get('white_ratio', 0.0):.3f}, "
        f"color={metrics.get('colorfulness', 0.0):.2f}, "
        f"edge={metrics.get('edge_density', 0.0):.3f}, "
        f"text_rows={metrics.get('text_rows_ratio', 0.0):.3f}, "
        f"midtone={metrics.get('midtone_ratio', 0.0):.3f}, "
        f"preview={metrics.get('size_preview', 'n/a')}, "
        f"aspect={metrics.get('aspect', 0.0):.3f}\n"
    )
    safe_log_append(log_path, text)


def log_error(log_path: Path, src: Path, err: str):
    text = (
        "\nERROR:\n"
        f"  FILE: {src}\n"
        f"  ERR:  {err}\n"
    )
    safe_log_append(log_path, text)


def log_checkpoint(log_path: Path, processed: int, planned: int, hits: int, passes: int, errors: int,
                   free_space_text: str, elapsed: str):
    text = (
        "\nCHECKPOINT:\n"
        f"  TS:            {datetime.now().isoformat(timespec='seconds')}\n"
        f"  PROCESSED:     {processed:,}/{planned:,}\n"
        f"  HITS:          {hits:,}\n"
        f"  PASSES:        {passes:,}\n"
        f"  ERRORS:        {errors:,}\n"
        f"  ELAPSED:       {elapsed}\n"
        f"  FREE SPACE:    {free_space_text}\n"
    )
    safe_log_append(log_path, text)


# ============================================================
# DISK / SIZE HELPERS
# ============================================================

def get_disk_usage(path: Path):
    return shutil.disk_usage(path)


def get_free_space_gb(path: Path) -> float:
    total, used, free = shutil.disk_usage(path)
    return free / (1024 ** 3)


def get_folder_size_bytes(root: Path, skip_dir: Path = None) -> int:
    total = 0
    skip_resolved = skip_dir.resolve() if skip_dir else None

    for dirpath, dirnames, filenames in os.walk(root):
        current_dir = Path(dirpath).resolve()

        if skip_resolved is not None:
            dirnames[:] = [
                d for d in dirnames
                if (current_dir / d).resolve() != skip_resolved
            ]

        for filename in filenames:
            fp = current_dir / filename
            try:
                total += fp.stat().st_size
            except (FileNotFoundError, PermissionError, OSError):
                pass

    return total


# ============================================================
# PROMPTS / UI
# ============================================================

def prompt_path() -> Path:
    while True:
        raw = input("Folder to use: ").strip().strip('"').strip("'")
        if not raw:
            safe_print("Please enter a folder path.")
            continue

        try:
            p = Path(raw).expanduser().resolve()
        except Exception as e:
            safe_print(f"Could not resolve that path: {e}")
            continue

        if not p.exists():
            safe_print("That path does not exist.")
            continue
        if not p.is_dir():
            safe_print("That path is not a folder.")
            continue
        return p


def prompt_yes_no(msg: str, default: bool = True) -> bool:
    suffix = "[Y/n]" if default else "[y/N]"
    while True:
        ans = input(f"{msg} {suffix}: ").strip().lower()
        if not ans:
            return default
        if ans in {"y", "yes"}:
            return True
        if ans in {"n", "no"}:
            return False
        safe_print("Please answer y or n.")


def prompt_main_mode() -> str:
    while True:
        safe_print()
        safe_print("Select mode:")
        safe_print("  (S)can mode")
        safe_print("  (C)ache maintenance")
        ans = input("Choice (s/c): ").strip().lower()
        if ans in {"s", "scan"}:
            return "scan"
        if ans in {"c", "cache"}:
            return "cache"
        safe_print("Please enter s or c.")


def prompt_scan_mode() -> str:
    while True:
        safe_print()
        safe_print("Scan modes:")
        safe_print("  1. Full scan")
        safe_print("  2. Retry only previous errors")
        safe_print("  3. Scan only new/changed files")
        safe_print("  4. Resume previous scan")
        ans = input("Choice (1-4): ").strip()
        if ans == "1":
            return "full"
        if ans == "2":
            return "retry_errors"
        if ans == "3":
            return "new_changed"
        if ans == "4":
            return "resume"
        safe_print("Please enter 1, 2, 3, or 4.")


def prompt_action_mode() -> str:
    while True:
        ans = input("Action mode: move, copy, or dry-run? (m/c/d): ").strip().lower()
        if ans in {"m", "move"}:
            return "move"
        if ans in {"c", "copy"}:
            safe_print()
            safe_print(COPY_WARNING_VERBATIM)
            safe_print()
            okay = prompt_yes_no("Are you sure you want COPY mode?", default=False)
            if okay:
                return "copy"
            safe_print("Returning to action selection.\n")
            continue
        if ans in {"d", "dry", "dry-run", "dryrun"}:
            return "dry-run"
        safe_print("Please enter m, c, or d.")


def prompt_min_free_space(default_gb: float) -> float:
    while True:
        raw = input(f"Minimum free space guard in GB [{default_gb}]: ").strip()
        if not raw:
            return float(default_gb)
        try:
            val = float(raw)
            if val < 0:
                safe_print("Enter a non-negative number.")
                continue
            return val
        except ValueError:
            safe_print("Please enter a valid number.")


def prompt_cache_menu() -> str:
    while True:
        safe_print()
        safe_print("Cache maintenance:")
        safe_print("  1. Inspect cache summary")
        safe_print("  2. Show last run info")
        safe_print("  3. Vacuum cache")
        safe_print("  4. Forget cached passes")
        safe_print("  5. Wipe cache")
        safe_print("  6. Exit")
        ans = input("Choice (1-6): ").strip()
        if ans == "1":
            return "inspect"
        if ans == "2":
            return "last_run"
        if ans == "3":
            return "vacuum"
        if ans == "4":
            return "forget_passes"
        if ans == "5":
            return "wipe"
        if ans == "6":
            return "exit"
        safe_print("Please enter 1, 2, 3, 4, 5, or 6.")


def prompt_bucket_config() -> dict:
    safe_print()
    safe_print("Bucket toggles:")
    return {
        BUCKET_BLACK: prompt_yes_no("Quarantine black/redacted pages?", True),
        BUCKET_DOCS: prompt_yes_no("Quarantine likely document pages?", True),
        BUCKET_REVIEW_HIGH: prompt_yes_no("Quarantine high-confidence review items?", True),
        BUCKET_REVIEW_LOW: prompt_yes_no("Quarantine low-confidence review items?", True),
    }


# ============================================================
# FILE HELPERS
# ============================================================

def safe_relpath(path: Path, base: Path) -> Path:
    try:
        return path.relative_to(base)
    except Exception:
        return Path(path.name)


def ensure_unique_path(dst: Path) -> Path:
    if not dst.exists():
        return dst
    stem = dst.stem
    suffix = dst.suffix
    parent = dst.parent
    counter = 1
    while True:
        candidate = parent / f"{stem}_{counter}{suffix}"
        if not candidate.exists():
            return candidate
        counter += 1


def list_image_files(root: Path, quarantine_dir: Path):
    quarantine_dir = quarantine_dir.resolve()
    for dirpath, dirnames, filenames in os.walk(root):
        current_dir = Path(dirpath).resolve()
        dirnames[:] = [
            d for d in dirnames
            if (current_dir / d).resolve() != quarantine_dir
        ]
        for name in filenames:
            p = Path(dirpath) / name
            if p.suffix.lower() in SUPPORTED_EXTS:
                yield p.resolve()


def get_file_signature(path: Path):
    st = path.stat()
    return st.st_size, st.st_mtime_ns


# ============================================================
# CACHE (SQLITE)
# ============================================================

def init_cache_db(db_path: Path):
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS scan_cache (
            path TEXT PRIMARY KEY,
            size INTEGER NOT NULL,
            mtime_ns INTEGER NOT NULL,
            status TEXT NOT NULL,
            bucket TEXT,
            score REAL,
            confidence TEXT,
            detail TEXT,
            action_mode TEXT,
            quarantine_path TEXT,
            run_ts TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_scan_cache_status
        ON scan_cache(status)
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS run_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            scan_root TEXT NOT NULL,
            scan_mode TEXT NOT NULL,
            action_mode TEXT,
            status TEXT NOT NULL,
            stop_reason TEXT,
            files_in_scope INTEGER DEFAULT 0,
            files_planned INTEGER DEFAULT 0,
            hits INTEGER DEFAULT 0,
            passes INTEGER DEFAULT 0,
            errors INTEGER DEFAULT 0,
            actioned INTEGER DEFAULT 0
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS meta (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """)
    conn.commit()
    return conn


def flush_cache(conn: sqlite3.Connection):
    try:
        conn.commit()
    except Exception:
        pass


def close_cache(conn: sqlite3.Connection):
    if conn is None:
        return
    try:
        conn.commit()
    except Exception:
        pass
    try:
        conn.close()
    except Exception:
        pass


def cache_lookup(conn: sqlite3.Connection, path: str):
    cur = conn.execute(
        """
        SELECT size, mtime_ns, status, bucket, score, confidence, detail, action_mode, quarantine_path
        FROM scan_cache
        WHERE path = ?
        """,
        (path,)
    )
    return cur.fetchone()


def cache_upsert(conn: sqlite3.Connection, path: str, size: int, mtime_ns: int, status: str,
                 bucket: str = None, score: float = None, confidence: str = None,
                 detail: str = None, action_mode: str = None, quarantine_path: str = None):
    conn.execute(
        """
        INSERT INTO scan_cache (
            path, size, mtime_ns, status, bucket, score, confidence, detail, action_mode, quarantine_path, run_ts
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(path) DO UPDATE SET
            size=excluded.size,
            mtime_ns=excluded.mtime_ns,
            status=excluded.status,
            bucket=excluded.bucket,
            score=excluded.score,
            confidence=excluded.confidence,
            detail=excluded.detail,
            action_mode=excluded.action_mode,
            quarantine_path=excluded.quarantine_path,
            run_ts=excluded.run_ts
        """,
        (
            path, size, mtime_ns, status, bucket, score, confidence,
            detail, action_mode, quarantine_path,
            datetime.now().isoformat(timespec="seconds"),
        )
    )


def start_run_history(conn: sqlite3.Connection, scan_root: str, scan_mode: str, action_mode: str,
                      files_in_scope: int, files_planned: int):
    cur = conn.execute(
        """
        INSERT INTO run_history (
            started_at, scan_root, scan_mode, action_mode, status,
            files_in_scope, files_planned
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            datetime.now().isoformat(timespec="seconds"),
            scan_root, scan_mode, action_mode, RUN_STATUS_RUNNING,
            files_in_scope, files_planned
        )
    )
    run_id = cur.lastrowid
    conn.execute(
        """
        INSERT INTO meta (key, value) VALUES ('last_run_id', ?)
        ON CONFLICT(key) DO UPDATE SET value=excluded.value
        """,
        (str(run_id),)
    )
    conn.commit()
    return run_id


def finish_run_history(conn: sqlite3.Connection, run_id: int, status: str, stop_reason: str,
                       hits: int, passes: int, errors: int, actioned: int):
    conn.execute(
        """
        UPDATE run_history
        SET finished_at = ?, status = ?, stop_reason = ?, hits = ?, passes = ?,
            errors = ?, actioned = ?
        WHERE id = ?
        """,
        (
            datetime.now().isoformat(timespec="seconds"),
            status, stop_reason, hits, passes, errors, actioned, run_id
        )
    )
    conn.commit()


def get_last_run(conn: sqlite3.Connection):
    cur = conn.execute("""
        SELECT id, started_at, finished_at, scan_root, scan_mode, action_mode,
               status, stop_reason, files_in_scope, files_planned,
               hits, passes, errors, actioned
        FROM run_history
        ORDER BY id DESC
        LIMIT 1
    """)
    return cur.fetchone()


def get_cache_summary(conn: sqlite3.Connection):
    total_cur = conn.execute("SELECT COUNT(*) FROM scan_cache")
    total = total_cur.fetchone()[0]
    rows = conn.execute("""
        SELECT status, COUNT(*)
        FROM scan_cache
        GROUP BY status
        ORDER BY status
    """).fetchall()
    return total, rows


def get_bucket_summary(conn: sqlite3.Connection):
    rows = conn.execute("""
        SELECT bucket, COUNT(*)
        FROM scan_cache
        WHERE bucket IS NOT NULL
        GROUP BY bucket
        ORDER BY bucket
    """).fetchall()
    return rows


def vacuum_cache(conn: sqlite3.Connection):
    conn.commit()
    conn.execute("VACUUM")
    conn.commit()


def wipe_cache(conn: sqlite3.Connection):
    conn.execute("DELETE FROM scan_cache")
    conn.execute("DELETE FROM run_history")
    conn.execute("DELETE FROM meta")
    conn.commit()


def forget_cached_passes(conn: sqlite3.Connection):
    conn.execute("DELETE FROM scan_cache WHERE status = 'pass'")
    conn.commit()


def cache_state_snapshot(conn: sqlite3.Connection, scan_root: Path):
    total, status_rows = get_cache_summary(conn)
    bucket_rows = get_bucket_summary(conn)
    last_run = get_last_run(conn)

    safe_print()
    safe_print("Cache state snapshot:")
    safe_print(f"  Cache DB exists:         yes")
    safe_print(f"  Target folder:           {scan_root}")
    safe_print(f"  Total cached entries:    {total:,}")

    if status_rows:
        safe_print("  Cached statuses:")
        for status, count in status_rows:
            safe_print(f"    {status:<16} {count:,}")
    else:
        safe_print("  Cached statuses:         none")

    if bucket_rows:
        safe_print("  Cached buckets:")
        for bucket, count in bucket_rows:
            safe_print(f"    {bucket:<16} {count:,}")

    hit_rows = conn.execute("""
        SELECT path, quarantine_path
        FROM scan_cache
        WHERE status IN ('hit_black', 'hit_docs', 'hit_review_high', 'hit_review_low')
    """).fetchall()
    missing_q = 0
    for _, qpath in hit_rows:
        if not qpath:
            missing_q += 1
        else:
            try:
                if not Path(qpath).exists():
                    missing_q += 1
            except Exception:
                missing_q += 1
    safe_print(f"  Cached hits missing destination files: {missing_q:,}")

    if last_run:
        (
            run_id, started_at, finished_at, scan_root_db, scan_mode,
            action_mode, status, stop_reason, files_in_scope,
            files_planned, hits, passes, errors, actioned
        ) = last_run
        safe_print("  Last run:")
        safe_print(f"    Run ID:               {run_id}")
        safe_print(f"    Started:              {started_at}")
        safe_print(f"    Finished:             {finished_at or 'not finalized'}")
        safe_print(f"    Root:                 {scan_root_db}")
        safe_print(f"    Scan mode:            {scan_mode}")
        safe_print(f"    Action mode:          {action_mode}")
        safe_print(f"    Status:               {status}")
        safe_print(f"    Stop reason:          {stop_reason or '(none)'}")
        safe_print(f"    Files in scope:       {files_in_scope:,}")
        safe_print(f"    Files planned:        {files_planned:,}")
        safe_print(f"    Hits:                 {hits:,}")
        safe_print(f"    Passes:               {passes:,}")
        safe_print(f"    Errors:               {errors:,}")
        safe_print(f"    Actioned:             {actioned:,}")
    else:
        safe_print("  Last run:                none")


def build_scan_plan(image_files, conn: sqlite3.Connection, scan_mode: str):
    to_scan = []
    skipped_cached_pass = 0
    skipped_cached_hit = 0
    skipped_missing_quarantine = 0
    retrying_cached_error = 0

    for path in image_files:
        try:
            size, mtime_ns = get_file_signature(path)
        except (FileNotFoundError, PermissionError, OSError):
            if scan_mode in {"full", "resume", "new_changed"}:
                to_scan.append(path)
            continue

        row = cache_lookup(conn, str(path))

        if scan_mode == "full":
            to_scan.append(path)
            continue

        if row is None:
            if scan_mode in {"new_changed", "resume"}:
                to_scan.append(path)
            continue

        cached_size, cached_mtime_ns, status, bucket, score, confidence, detail, action_mode, quarantine_path = row
        unchanged = (cached_size == size and cached_mtime_ns == mtime_ns)

        if scan_mode == "retry_errors":
            if status == "error":
                retrying_cached_error += 1
                to_scan.append(path)
            continue

        if scan_mode == "new_changed":
            if not unchanged:
                to_scan.append(path)
                continue
            if status == "pass":
                skipped_cached_pass += 1
            elif status in {"hit_black", "hit_docs", "hit_review_high", "hit_review_low"}:
                skipped_cached_hit += 1
            continue

        if scan_mode == "resume":
            if not unchanged:
                to_scan.append(path)
                continue

            if status == "pass":
                skipped_cached_pass += 1
                continue

            if status in {"hit_black", "hit_docs", "hit_review_high", "hit_review_low"}:
                if quarantine_path:
                    try:
                        if Path(quarantine_path).exists():
                            skipped_cached_hit += 1
                            continue
                    except Exception:
                        pass
                skipped_missing_quarantine += 1
                to_scan.append(path)
                continue

            if status == "error":
                retrying_cached_error += 1
                to_scan.append(path)
                continue

            to_scan.append(path)
            continue

    return {
        "to_scan": to_scan,
        "skipped_cached_pass": skipped_cached_pass,
        "skipped_cached_hit": skipped_cached_hit,
        "skipped_missing_quarantine": skipped_missing_quarantine,
        "retrying_cached_error": retrying_cached_error,
    }


# ============================================================
# IMAGE ANALYSIS
# ============================================================

def resize_for_analysis(img: Image.Image) -> Image.Image:
    img = img.copy()
    img.thumbnail((ANALYZE_MAX_DIM, ANALYZE_MAX_DIM), Image.Resampling.LANCZOS)
    return img


def rgb_to_colorfulness(arr_rgb: np.ndarray) -> float:
    r = arr_rgb[..., 0].astype(np.float32)
    g = arr_rgb[..., 1].astype(np.float32)
    b = arr_rgb[..., 2].astype(np.float32)
    rg = np.abs(r - g)
    yb = np.abs(0.5 * (r + g) - b)
    std_root = math.sqrt(float(np.std(rg) ** 2 + np.std(yb) ** 2))
    mean_root = math.sqrt(float(np.mean(rg) ** 2 + np.mean(yb) ** 2))
    return std_root + 0.3 * mean_root


def edge_density(gray: np.ndarray) -> float:
    gx = np.abs(np.diff(gray, axis=1))
    gy = np.abs(np.diff(gray, axis=0))
    if gx.size == 0 or gy.size == 0:
        return 0.0
    ex = float(np.mean(gx > 18))
    ey = float(np.mean(gy > 18))
    return (ex + ey) / 2.0


def detect_text_line_pattern(gray: np.ndarray):
    h, w = gray.shape
    if h < 20 or w < 20:
        return 0.0, 0.0

    dark = gray < 165
    row_dark_ratio = dark.mean(axis=1)
    text_rows = (row_dark_ratio > 0.03) & (row_dark_ratio < 0.65)
    text_rows_ratio = float(text_rows.mean())
    transitions = np.abs(np.diff(text_rows.astype(np.int8))).sum()
    transitions_ratio = float(transitions / max(1, h - 1))
    return text_rows_ratio, transitions_ratio


def margin_structure_score(gray: np.ndarray):
    h, w = gray.shape
    if h < 40 or w < 40:
        return 0.0

    top = gray[: max(1, h // 12), :]
    bottom = gray[-max(1, h // 12):, :]
    left = gray[:, : max(1, w // 12)]
    right = gray[:, -max(1, w // 12):]
    center = gray[h // 8: h - h // 8, w // 8: w - w // 8]

    edge_white = np.mean(np.concatenate([top.ravel(), bottom.ravel(), left.ravel(), right.ravel()]) >= 220)
    center_dark = np.mean(center < 180) if center.size else 0.0

    return float((edge_white * 0.7) + (center_dark * 0.3))


def monochrome_score(rgb: np.ndarray) -> float:
    if rgb.ndim != 3 or rgb.shape[2] != 3:
        return 0.0
    diff_rg = np.mean(np.abs(rgb[..., 0].astype(np.int16) - rgb[..., 1].astype(np.int16)))
    diff_rb = np.mean(np.abs(rgb[..., 0].astype(np.int16) - rgb[..., 2].astype(np.int16)))
    diff_gb = np.mean(np.abs(rgb[..., 1].astype(np.int16) - rgb[..., 2].astype(np.int16)))
    mean_diff = (diff_rg + diff_rb + diff_gb) / 3.0
    return float(max(0.0, 1.0 - (mean_diff / 64.0)))


def redaction_slab_score(gray: np.ndarray):
    h, w = gray.shape
    if h < 20 or w < 20:
        return 0.0
    dark = (gray <= BLACK_PIXEL_THRESHOLD).astype(np.uint8)
    row_dark = dark.mean(axis=1)
    col_dark = dark.mean(axis=0)

    heavy_rows = np.mean(row_dark > 0.90)
    heavy_cols = np.mean(col_dark > 0.90)
    overall = float(np.mean(dark))
    return float((overall * 0.6) + (heavy_rows * 0.2) + (heavy_cols * 0.2))


def classify_confidence(score: float) -> str:
    if score >= 6.0:
        return "strong"
    if score >= 4.25:
        return "medium"
    return "weak"


def analyze_image(path_str: str):
    path = Path(path_str)

    try:
        with Image.open(path) as img:
            original_w, original_h = img.size

            if original_w < TINY_IMAGE_IGNORE_DIM and original_h < TINY_IMAGE_IGNORE_DIM:
                return {
                    "path": path_str,
                    "ok": True,
                    "classify": False,
                    "bucket": None,
                    "score": 0.0,
                    "confidence": "tiny-ignore",
                    "reasons": [f"tiny image ({original_w}x{original_h}) auto-pass"],
                    "metrics": {"size_preview": f"{original_w}x{original_h}"}
                }

            if img.mode not in ("RGB", "RGBA", "L"):
                img = img.convert("RGB")

            preview = resize_for_analysis(img)

            if preview.mode == "RGBA":
                bg = Image.new("RGBA", preview.size, (255, 255, 255, 255))
                preview = Image.alpha_composite(bg, preview).convert("RGB")

            if preview.mode == "L":
                gray_img = preview
                rgb_img = preview.convert("RGB")
            else:
                rgb_img = preview.convert("RGB")
                gray_img = preview.convert("L")

            gray = np.asarray(gray_img, dtype=np.uint8)
            rgb = np.asarray(rgb_img, dtype=np.uint8)

        h, w = gray.shape
        total_pixels = h * w

        if total_pixels == 0:
            return {
                "path": path_str,
                "ok": True,
                "classify": False,
                "bucket": None,
                "score": 0.0,
                "confidence": "weak",
                "reasons": ["empty image"],
                "metrics": {}
            }

        black_ratio = float(np.mean(gray <= BLACK_PIXEL_THRESHOLD))
        white_ratio = float(np.mean(gray >= WHITE_PIXEL_THRESHOLD))
        mean_gray = float(np.mean(gray))
        std_gray = float(np.std(gray))
        edge = edge_density(gray)
        colorfulness = rgb_to_colorfulness(rgb)
        text_rows_ratio, transitions_ratio = detect_text_line_pattern(gray)
        midtone_ratio = float(np.mean((gray > 60) & (gray < 195)))
        aspect = max(w, h) / max(1, min(w, h))
        mono_score = monochrome_score(rgb)
        slab_score = redaction_slab_score(gray)
        margin_score = margin_structure_score(gray)

        reasons = []
        score = 0.0
        bucket = None

        # Black / redacted
        if black_ratio >= MOSTLY_BLACK_RATIO:
            score += 5.0
            reasons.append(f"mostly black page ({black_ratio:.1%} black)")
            bucket = BUCKET_BLACK
        elif black_ratio >= 0.85 and std_gray < 30:
            score += 4.0
            reasons.append(f"very dark low-variation page ({black_ratio:.1%} black)")
            bucket = BUCKET_BLACK
        elif slab_score >= 0.75 and colorfulness < 8.0:
            score += 3.8
            reasons.append(f"redaction slab pattern ({slab_score:.2f})")
            bucket = BUCKET_BLACK
        elif black_ratio >= 0.70 and colorfulness < 8.0 and midtone_ratio < 0.12:
            score += 3.5
            reasons.append("dark low-color image, likely heavy redaction page")
            bucket = BUCKET_BLACK

        # Doc-like
        if white_ratio >= MOSTLY_WHITE_RATIO and colorfulness < LOW_COLORFULNESS_STD:
            score += 1.5
            reasons.append(f"mostly white and low-color ({white_ratio:.1%} white)")

        if colorfulness < LOW_COLORFULNESS_STD:
            score += 1.0
            reasons.append(f"low colorfulness ({colorfulness:.1f})")

        if colorfulness < 7.0:
            score += 0.5
            reasons.append("extremely low color")

        if mono_score > 0.82:
            score += 0.8
            reasons.append(f"near-monochrome image ({mono_score:.2f})")

        if (
            MIN_TEXT_ROWS_RATIO <= text_rows_ratio <= MAX_TEXT_ROWS_RATIO
            and white_ratio > 0.35
            and black_ratio < 0.50
        ):
            score += 1.8
            reasons.append(
                f"text-line pattern detected "
                f"(text_rows={text_rows_ratio:.1%}, transitions={transitions_ratio:.2f})"
            )

        if edge > 0.04 and edge < 0.28 and colorfulness < 12.0 and white_ratio > 0.30:
            score += 0.9
            reasons.append("document-like edges on pale background")

        if colorfulness < 10.0 and midtone_ratio < 0.25 and (white_ratio + black_ratio) > 0.70:
            score += 0.8
            reasons.append("bimodal black/white page structure")

        if white_ratio > 0.55 and text_rows_ratio > 0.08:
            score += 0.8
            reasons.append("light page with repeated dark row bands")

        if 1.15 <= aspect <= 1.65 and colorfulness < 12.0:
            score += 0.3
            reasons.append("page-like aspect ratio")

        if margin_score > 0.55 and white_ratio > 0.35:
            score += 0.7
            reasons.append(f"white-margin / darker-center structure ({margin_score:.2f})")

        doc_like = (
            (text_rows_ratio > 0.08 and white_ratio > 0.35 and colorfulness < 14.0)
            or (white_ratio > 0.70 and colorfulness < 10.0)
            or ("document-like edges on pale background" in reasons)
            or (margin_score > 0.60 and mono_score > 0.75)
        )

        if bucket is None and doc_like:
            bucket = BUCKET_DOCS

        likely_photo = False
        if colorfulness > 18.0 and midtone_ratio > 0.30:
            likely_photo = True
        if edge > 0.18 and colorfulness > 15.0:
            likely_photo = True
        if white_ratio < 0.20 and black_ratio < 0.35 and colorfulness > 20.0:
            likely_photo = True

        if likely_photo:
            score -= 1.8
            reasons.append("photo-like characteristics detected")

        classify = score >= QUARANTINE_SCORE_THRESHOLD

        if classify and bucket is None:
            bucket = BUCKET_REVIEW_HIGH if score >= REVIEW_HIGH_SCORE_THRESHOLD else BUCKET_REVIEW_LOW

        confidence = classify_confidence(score) if classify else "weak"

        metrics = {
            "size_preview": f"{w}x{h}",
            "black_ratio": black_ratio,
            "white_ratio": white_ratio,
            "mean_gray": mean_gray,
            "std_gray": std_gray,
            "edge_density": edge,
            "colorfulness": colorfulness,
            "text_rows_ratio": text_rows_ratio,
            "transitions_ratio": transitions_ratio,
            "midtone_ratio": midtone_ratio,
            "aspect": aspect,
            "mono_score": mono_score,
            "slab_score": slab_score,
            "margin_score": margin_score,
        }

        return {
            "path": path_str,
            "ok": True,
            "classify": classify,
            "bucket": bucket,
            "score": score,
            "confidence": confidence,
            "reasons": reasons,
            "metrics": metrics,
        }

    except (UnidentifiedImageError, OSError, ValueError) as e:
        return {
            "path": path_str,
            "ok": False,
            "error": f"{type(e).__name__}: {e}",
        }
    except Exception as e:
        return {
            "path": path_str,
            "ok": False,
            "error": f"Unexpected {type(e).__name__}: {e}",
        }


# ============================================================
# QUARANTINE ACTIONS
# ============================================================

def build_quarantine_destination(src: Path, scan_root: Path, quarantine_root: Path, bucket: str):
    rel = safe_relpath(src, scan_root)
    intended = quarantine_root / bucket / rel
    intended.parent.mkdir(parents=True, exist_ok=True)
    final_dst = ensure_unique_path(intended)
    return intended, final_dst


def safe_copy_then_verify(src: Path, dst: Path):
    tmp_dst = dst.with_name(dst.name + ".part")
    if tmp_dst.exists():
        try:
            tmp_dst.unlink()
        except Exception:
            pass

    shutil.copy2(str(src), str(tmp_dst))
    src_size = src.stat().st_size
    dst_size = tmp_dst.stat().st_size
    if src_size != dst_size:
        raise OSError(f"copy verification failed (src={src_size}, tmp={dst_size})")
    tmp_dst.replace(dst)


def quarantine_file(src: Path, scan_root: Path, quarantine_root: Path, bucket: str, action_mode: str):
    intended, final_dst = build_quarantine_destination(src, scan_root, quarantine_root, bucket)

    if action_mode == "move":
        safe_copy_then_verify(src, final_dst)
        src.unlink()
    elif action_mode == "copy":
        safe_copy_then_verify(src, final_dst)
    elif action_mode == "dry-run":
        pass
    else:
        raise ValueError(f"Unknown action mode: {action_mode}")

    return intended, final_dst


# ============================================================
# PROGRESS / OUTPUT
# ============================================================

_last_status_len = 0


def render_progress_bar(done: int, total: int, width: int = PROGRESS_BAR_WIDTH) -> str:
    if total <= 0:
        return "[" + ("-" * width) + "]"
    ratio = max(0.0, min(1.0, done / total))
    filled = int(ratio * width)
    return "[" + ("#" * filled) + ("-" * (width - filled)) + "]"


def refresh_status_line(done: int, total: int, start_time: float, hits: int, passes: int,
                        errors: int, last_label: str):
    global _last_status_len

    elapsed = time.time() - start_time
    rate = done / elapsed if elapsed > 0 else 0.0
    remaining = total - done
    eta = remaining / rate if rate > 0 else float("inf")
    pct = (done / total * 100.0) if total else 0.0
    bar = render_progress_bar(done, total)

    line = (
        f"{bar} {done:,}/{total:,} {pct:6.2f}% | "
        f"hits={hits:,} pass={passes:,} err={errors:,} | "
        f"rate={format_rate(done, elapsed)} | "
        f"elapsed={format_seconds(elapsed)} | "
        f"eta={format_seconds(eta)} | "
        f"{last_label}"
    )

    pad = ""
    if len(line) < _last_status_len:
        pad = " " * (_last_status_len - len(line))

    sys.stdout.write("\r" + line + pad)
    sys.stdout.flush()
    _last_status_len = len(line)


def clear_status_line():
    global _last_status_len
    if _last_status_len > 0:
        sys.stdout.write("\r" + (" " * _last_status_len) + "\r")
        sys.stdout.flush()
        _last_status_len = 0


def print_event_line(msg: str):
    clear_status_line()
    safe_print(msg)


# ============================================================
# CSV REPORT
# ============================================================

def csv_init(csv_path: Path):
    with open(csv_path, "w", newline="", encoding="utf-8", errors="replace") as f:
        writer = csv.writer(f)
        writer.writerow([
            "timestamp", "source_path", "result_status", "bucket", "score", "confidence",
            "action_mode", "quarantine_path", "reason_summary"
        ])


def csv_append_row(csv_path: Path, row: list):
    try:
        with open(csv_path, "a", newline="", encoding="utf-8", errors="replace") as f:
            writer = csv.writer(f)
            writer.writerow(row)
    except Exception as e:
        safe_print(f"[WARN] CSV write failed: {e}")


# ============================================================
# AUTO WORKER SELECTION
# ============================================================

def choose_worker_count() -> int:
    count = cpu_count() or 2
    return max(1, min(count, AUTO_WORKER_CAP))


# ============================================================
# CACHE MAINTENANCE
# ============================================================

def do_cache_maintenance(scan_root: Path):
    db_path = scan_root / CACHE_DB_NAME

    if not db_path.exists():
        safe_print()
        safe_print(f"No cache DB found yet at: {db_path}")
        return

    conn = None
    try:
        conn = init_cache_db(db_path)

        while True:
            choice = prompt_cache_menu()

            if choice == "exit":
                break

            if choice == "inspect":
                total, rows = get_cache_summary(conn)
                safe_print()
                safe_print(f"Cache DB: {db_path}")
                safe_print(f"Total cached entries: {total:,}")
                if not rows:
                    safe_print("No cached scan entries found.")
                else:
                    safe_print("Status counts:")
                    for status, count in rows:
                        safe_print(f"  {status:<18} {count:,}")
                buckets = get_bucket_summary(conn)
                if buckets:
                    safe_print("Bucket counts:")
                    for bucket, count in buckets:
                        safe_print(f"  {bucket:<18} {count:,}")

            elif choice == "last_run":
                row = get_last_run(conn)
                safe_print()
                if not row:
                    safe_print("No run history found.")
                else:
                    (
                        run_id, started_at, finished_at, scan_root_db, scan_mode,
                        action_mode, status, stop_reason, files_in_scope,
                        files_planned, hits, passes, errors, actioned
                    ) = row
                    safe_print("Last run info:")
                    safe_print(f"  Run ID:             {run_id}")
                    safe_print(f"  Started:            {started_at}")
                    safe_print(f"  Finished:           {finished_at or 'still marked running / not finalized'}")
                    safe_print(f"  Scan root:          {scan_root_db}")
                    safe_print(f"  Scan mode:          {scan_mode}")
                    safe_print(f"  Action mode:        {action_mode}")
                    safe_print(f"  Status:             {status}")
                    safe_print(f"  Stop reason:        {stop_reason or '(none)'}")
                    safe_print(f"  Files in scope:     {files_in_scope:,}")
                    safe_print(f"  Files planned:      {files_planned:,}")
                    safe_print(f"  Hits:               {hits:,}")
                    safe_print(f"  Passes:             {passes:,}")
                    safe_print(f"  Errors:             {errors:,}")
                    safe_print(f"  Files actioned:     {actioned:,}")

            elif choice == "vacuum":
                safe_print()
                safe_print("Vacuuming cache DB...")
                vacuum_cache(conn)
                safe_print("Vacuum complete.")

            elif choice == "forget_passes":
                safe_print()
                safe_print("This will remove cached PASS entries only.")
                if prompt_yes_no("Forget cached passes?", default=False):
                    forget_cached_passes(conn)
                    safe_print("Cached passes removed.")
                else:
                    safe_print("Canceled.")

            elif choice == "wipe":
                safe_print()
                safe_print("This will wipe cached scan state and run history for this folder.")
                if prompt_yes_no("Are you sure you want to wipe the cache?", default=False):
                    wipe_cache(conn)
                    safe_print("Cache wiped.")
                else:
                    safe_print("Wipe canceled.")

    except Exception as e:
        safe_print(f"Cache maintenance failed: {type(e).__name__}: {e}")
    finally:
        close_cache(conn)


# ============================================================
# SCAN MODE
# ============================================================

def should_quarantine_bucket(bucket: str, bucket_config: dict) -> bool:
    return bool(bucket_config.get(bucket, False))


def default_guard_for_action(action_mode: str) -> float:
    if action_mode == "copy":
        return DEFAULT_MIN_FREE_GB_COPY
    if action_mode == "dry-run":
        return DEFAULT_MIN_FREE_GB_DRY
    return DEFAULT_MIN_FREE_GB_MOVE


def maybe_open_quarantine_folder(path: Path):
    if os.name != "nt":
        return
    if prompt_yes_no("Open quarantine folder now?", default=False):
        try:
            os.startfile(str(path))
        except Exception as e:
            safe_print(f"Could not open folder: {e}")


def do_scan_mode(scan_root: Path):
    try:
        scan_drive_usage = get_disk_usage(scan_root)
        safe_print()
        safe_print(
            f"Target drive free space: "
            f"{format_bytes(scan_drive_usage.free)} / {format_bytes(scan_drive_usage.total)}"
        )
    except Exception as e:
        safe_print()
        safe_print(f"Could not read target drive usage: {e}")
        safe_print("Cannot safely continue without disk usage info.")
        return

    quarantine_root = scan_root / QUARANTINE_NAME
    db_path = scan_root / CACHE_DB_NAME

    safe_print("Calculating scan directory size...")
    try:
        scan_dir_size_bytes = get_folder_size_bytes(scan_root, skip_dir=quarantine_root)
    except Exception as e:
        safe_print(f"Failed to calculate folder size: {e}")
        return

    safe_print(f"Directory to scan: {scan_root}")
    safe_print(f"Directory size:    {human_folder_size_display(scan_dir_size_bytes)}")

    snapshot_conn = None
    try:
        snapshot_conn = init_cache_db(db_path)
        cache_state_snapshot(snapshot_conn, scan_root)
    except Exception as e:
        safe_print(f"[WARN] Could not load cache snapshot: {e}")
    finally:
        close_cache(snapshot_conn)

    safe_print()
    scan_mode = prompt_scan_mode()
    action_mode = prompt_action_mode()
    bucket_config = prompt_bucket_config()
    min_free_gb = prompt_min_free_space(default_guard_for_action(action_mode))
    workers = choose_worker_count()

    try:
        quarantine_root.mkdir(parents=True, exist_ok=True)
        (quarantine_root / BUCKET_BLACK).mkdir(parents=True, exist_ok=True)
        (quarantine_root / BUCKET_DOCS).mkdir(parents=True, exist_ok=True)
        (quarantine_root / BUCKET_REVIEW_HIGH).mkdir(parents=True, exist_ok=True)
        (quarantine_root / BUCKET_REVIEW_LOW).mkdir(parents=True, exist_ok=True)
    except Exception as e:
        safe_print(f"Failed to create quarantine directories: {e}")
        return

    log_path = scan_root / LOG_NAME
    csv_path = scan_root / CSV_REPORT_NAME

    write_log_header(
        log_path=log_path,
        scan_root=scan_root,
        scan_mode=scan_mode,
        action_mode=action_mode,
        workers=workers,
        min_free_gb=min_free_gb,
        scan_dir_size_bytes=scan_dir_size_bytes,
        initial_free_bytes=scan_drive_usage.free,
        bucket_config=bucket_config,
    )

    csv_init(csv_path)

    safe_print()
    safe_print("Buckets:")
    safe_print(f"  {BUCKET_BLACK:<14} enabled={bucket_config[BUCKET_BLACK]}")
    safe_print(f"  {BUCKET_DOCS:<14} enabled={bucket_config[BUCKET_DOCS]}")
    safe_print(f"  {BUCKET_REVIEW_HIGH:<14} enabled={bucket_config[BUCKET_REVIEW_HIGH]}")
    safe_print(f"  {BUCKET_REVIEW_LOW:<14} enabled={bucket_config[BUCKET_REVIEW_LOW]}")
    safe_print()
    safe_print(f"Scan mode:              {scan_mode}")
    safe_print(f"Action mode:            {action_mode}")
    safe_print(f"Worker processes:       {workers} (auto-selected)")
    safe_print(f"Free space guard:       {min_free_gb:.2f} GB")
    safe_print(f"Quarantine root:        {quarantine_root}")
    safe_print(f"Log file:               {log_path}")
    safe_print(f"Resume cache DB:        {db_path}")
    safe_print(f"CSV report:             {csv_path}")
    safe_print()

    if action_mode == "copy":
        safe_print("Copy mode reminder:")
        safe_print(COPY_WARNING_VERBATIM)
        safe_print(f"Current free space:     {format_bytes(scan_drive_usage.free)}")
        safe_print(f"Scan directory size:    {format_bytes(scan_dir_size_bytes)}")
        safe_print("Copy mode may temporarily require space approaching the size of quarantined matches.")
        safe_print()

    if action_mode == "dry-run":
        safe_print("Dry-run mode reminder:")
        safe_print("No files will be moved or copied. Classification and reports will still be generated.")
        safe_print()

    if not prompt_yes_no("Proceed with scan?", True):
        safe_print("Canceled.")
        return

    cache_conn = None
    pool = None
    run_id = None

    try:
        cache_conn = init_cache_db(db_path)
    except Exception as e:
        safe_print(f"Failed to initialize cache DB: {e}")
        return

    try:
        image_files = list(list_image_files(scan_root, quarantine_root))
    except Exception as e:
        close_cache(cache_conn)
        safe_print(f"Failed to enumerate image files: {e}")
        return

    total_seen = len(image_files)
    safe_print()
    safe_print(f"Found {total_seen:,} image files in scan scope.")
    safe_print("Building scan plan...")

    try:
        plan = build_scan_plan(image_files=image_files, conn=cache_conn, scan_mode=scan_mode)
    except Exception as e:
        close_cache(cache_conn)
        safe_print(f"Failed to build scan plan from cache: {e}")
        return

    to_scan = plan["to_scan"]
    skipped_cached_pass = plan["skipped_cached_pass"]
    skipped_cached_hit = plan["skipped_cached_hit"]
    skipped_missing_quarantine = plan["skipped_missing_quarantine"]
    retrying_cached_error = plan["retrying_cached_error"]

    safe_print(f"Will scan now:            {len(to_scan):,}")
    safe_print(f"Skipped cached pass:      {skipped_cached_pass:,}")
    safe_print(f"Skipped cached hit:       {skipped_cached_hit:,}")
    safe_print(f"Missing quarantined hit:  {skipped_missing_quarantine:,} (will be rescanned)")
    safe_print(f"Retrying cached errors:   {retrying_cached_error:,}")
    safe_print()

    if not to_scan:
        close_cache(cache_conn)
        safe_print("Nothing needs scanning for the selected mode.")
        return

    try:
        run_id = start_run_history(
            cache_conn,
            scan_root=str(scan_root),
            scan_mode=scan_mode,
            action_mode=action_mode,
            files_in_scope=total_seen,
            files_planned=len(to_scan),
        )
    except Exception as e:
        close_cache(cache_conn)
        safe_print(f"Failed to start run-history entry: {e}")
        return

    safe_print("Starting analysis...")
    safe_print()

    actioned = 0
    hits = 0
    passes = 0
    errors = 0

    black_count = 0
    docs_count = 0
    review_high_count = 0
    review_low_count = 0

    disabled_bucket_hits = 0
    dry_run_hits = 0

    stop_reason = None
    final_run_status = RUN_STATUS_COMPLETED
    start_time = time.time()
    last_cache_flush = time.time()
    last_checkpoint_processed = 0
    last_checkpoint_hits = 0
    last_label = "warming up"
    cache_dirty_ops = 0

    try:
        pool = Pool(processes=workers)

        for idx, result in enumerate(pool.imap_unordered(analyze_image, map(str, to_scan), chunksize=CHUNKSIZE), 1):
            try:
                free_now_gb = get_free_space_gb(scan_root)
            except Exception as e:
                stop_reason = f"Failed to read free space during run: {e}"
                final_run_status = RUN_STATUS_FAILED
                print_event_line(f"[STOP] {stop_reason}")
                pool.terminate()
                pool.join()
                pool = None
                break

            if free_now_gb < min_free_gb:
                stop_reason = (
                    f"Free space dropped below guard threshold: "
                    f"{free_now_gb:.2f} GB remaining < {min_free_gb:.2f} GB guard"
                )
                final_run_status = RUN_STATUS_STOPPED_GUARD
                print_event_line(f"[STOP] {stop_reason}")
                pool.terminate()
                pool.join()
                pool = None
                break

            src = Path(result.get("path", "<unknown>"))
            try:
                size, mtime_ns = get_file_signature(src)
            except (FileNotFoundError, PermissionError, OSError):
                size, mtime_ns = -1, -1

            if not result.get("ok", False):
                errors += 1
                err = result.get("error", "Unknown error")
                log_error(log_path, src, err)

                try:
                    cache_upsert(
                        cache_conn,
                        path=str(src), size=size, mtime_ns=mtime_ns,
                        status="error", detail=err, action_mode=action_mode
                    )
                    cache_dirty_ops += 1
                except Exception as e:
                    print_event_line(f"[ERROR] cache write failed for {src} | {e}")

                csv_append_row(csv_path, [
                    datetime.now().isoformat(timespec="seconds"),
                    str(src), "error", "", "", "", action_mode, "", err
                ])

                print_event_line(f"[ERROR] {src} | {err}")
                last_label = "error"
                refresh_status_line(idx, len(to_scan), start_time, hits, passes, errors, last_label)

            else:
                try:
                    score = result.get("score", 0.0)
                    bucket = result.get("bucket")
                    confidence = result.get("confidence", "")
                    reasons = "; ".join(result.get("reasons", []))
                    status = "pass"
                    quarantine_path = ""
                    bucket_used = ""
                    intended_dst = None
                    final_dst = None

                    if result["classify"] and bucket and should_quarantine_bucket(bucket, bucket_config):
                        if action_mode == "dry-run":
                            dry_run_hits += 1
                            hits += 1
                            actioned += 1
                            status = {
                                BUCKET_BLACK: "hit_black",
                                BUCKET_DOCS: "hit_docs",
                                BUCKET_REVIEW_HIGH: "hit_review_high",
                                BUCKET_REVIEW_LOW: "hit_review_low",
                            }[bucket]
                            bucket_used = bucket

                            if bucket == BUCKET_BLACK:
                                black_count += 1
                            elif bucket == BUCKET_DOCS:
                                docs_count += 1
                            elif bucket == BUCKET_REVIEW_HIGH:
                                review_high_count += 1
                            else:
                                review_low_count += 1

                            try:
                                cache_upsert(
                                    cache_conn,
                                    path=str(src), size=size, mtime_ns=mtime_ns,
                                    status=status, bucket=bucket, score=score,
                                    confidence=confidence, detail=reasons,
                                    action_mode=action_mode, quarantine_path=""
                                )
                                cache_dirty_ops += 1
                            except Exception as e:
                                print_event_line(f"[ERROR] cache write failed for {src} | {e}")

                            csv_append_row(csv_path, [
                                datetime.now().isoformat(timespec="seconds"),
                                str(src), status, bucket, f"{score:.2f}", confidence,
                                action_mode, "", reasons
                            ])

                            print_event_line(
                                f"[HIT-DRY] {bucket:<14} | score={score:.2f} | {src.name} | {reasons}"
                            )
                            last_label = f"dry-hit:{bucket}"

                        else:
                            intended_dst, final_dst = quarantine_file(
                                src=src,
                                scan_root=scan_root,
                                quarantine_root=quarantine_root,
                                bucket=bucket,
                                action_mode=action_mode,
                            )
                            hits += 1
                            actioned += 1
                            status = {
                                BUCKET_BLACK: "hit_black",
                                BUCKET_DOCS: "hit_docs",
                                BUCKET_REVIEW_HIGH: "hit_review_high",
                                BUCKET_REVIEW_LOW: "hit_review_low",
                            }[bucket]
                            bucket_used = bucket
                            quarantine_path = str(final_dst)

                            if bucket == BUCKET_BLACK:
                                black_count += 1
                            elif bucket == BUCKET_DOCS:
                                docs_count += 1
                            elif bucket == BUCKET_REVIEW_HIGH:
                                review_high_count += 1
                            else:
                                review_low_count += 1

                            log_action(log_path, src, intended_dst, final_dst, result, action_mode)

                            try:
                                cache_upsert(
                                    cache_conn,
                                    path=str(src), size=size, mtime_ns=mtime_ns,
                                    status=status, bucket=bucket, score=score,
                                    confidence=confidence, detail=reasons,
                                    action_mode=action_mode, quarantine_path=quarantine_path
                                )
                                cache_dirty_ops += 1
                            except Exception as e:
                                print_event_line(f"[ERROR] cache write failed for {src} | {e}")

                            csv_append_row(csv_path, [
                                datetime.now().isoformat(timespec="seconds"),
                                str(src), status, bucket, f"{score:.2f}", confidence,
                                action_mode, quarantine_path, reasons
                            ])

                            print_event_line(
                                f"[HIT] {bucket:<14} | {confidence:<6} | score={score:.2f} | {src.name} | {reasons}"
                            )
                            last_label = f"hit:{bucket}"

                    else:
                        passes += 1
                        if result["classify"] and bucket and not should_quarantine_bucket(bucket, bucket_config):
                            disabled_bucket_hits += 1
                            status = "pass"
                            reasons = f"bucket disabled -> pass | {reasons}"
                            last_label = f"pass(disabled:{bucket})"
                        else:
                            last_label = f"pass:{src.name}"

                        try:
                            cache_upsert(
                                cache_conn,
                                path=str(src), size=size, mtime_ns=mtime_ns,
                                status=status, score=score, confidence=confidence,
                                detail=reasons, action_mode=action_mode
                            )
                            cache_dirty_ops += 1
                        except Exception as e:
                            print_event_line(f"[ERROR] cache write failed for {src} | {e}")

                        csv_append_row(csv_path, [
                            datetime.now().isoformat(timespec="seconds"),
                            str(src), status, bucket or "", f"{score:.2f}", confidence,
                            action_mode, "", reasons
                        ])

                    refresh_status_line(idx, len(to_scan), start_time, hits, passes, errors, last_label)

                except FileNotFoundError as e:
                    errors += 1
                    msg = f"File disappeared during action: {e}"
                    log_error(log_path, src, msg)
                    try:
                        cache_upsert(
                            cache_conn,
                            path=str(src), size=size, mtime_ns=mtime_ns,
                            status="error", detail=msg, action_mode=action_mode
                        )
                        cache_dirty_ops += 1
                    except Exception as ce:
                        print_event_line(f"[ERROR] cache write failed for {src} | {ce}")
                    csv_append_row(csv_path, [
                        datetime.now().isoformat(timespec="seconds"),
                        str(src), "error", "", "", "", action_mode, "", msg
                    ])
                    print_event_line(f"[ERROR] {src} | {msg}")
                    refresh_status_line(idx, len(to_scan), start_time, hits, passes, errors, "action-error")

                except PermissionError as e:
                    errors += 1
                    msg = f"Permission error during action: {e}"
                    log_error(log_path, src, msg)
                    try:
                        cache_upsert(
                            cache_conn,
                            path=str(src), size=size, mtime_ns=mtime_ns,
                            status="error", detail=msg, action_mode=action_mode
                        )
                        cache_dirty_ops += 1
                    except Exception as ce:
                        print_event_line(f"[ERROR] cache write failed for {src} | {ce}")
                    csv_append_row(csv_path, [
                        datetime.now().isoformat(timespec="seconds"),
                        str(src), "error", "", "", "", action_mode, "", msg
                    ])
                    print_event_line(f"[ERROR] {src} | {msg}")
                    refresh_status_line(idx, len(to_scan), start_time, hits, passes, errors, "action-error")

                except OSError as e:
                    errors += 1
                    msg = f"OS error during action: {e}"
                    log_error(log_path, src, msg)
                    try:
                        cache_upsert(
                            cache_conn,
                            path=str(src), size=size, mtime_ns=mtime_ns,
                            status="error", detail=msg, action_mode=action_mode
                        )
                        cache_dirty_ops += 1
                    except Exception as ce:
                        print_event_line(f"[ERROR] cache write failed for {src} | {ce}")
                    csv_append_row(csv_path, [
                        datetime.now().isoformat(timespec="seconds"),
                        str(src), "error", "", "", "", action_mode, "", msg
                    ])
                    print_event_line(f"[ERROR] {src} | {msg}")
                    refresh_status_line(idx, len(to_scan), start_time, hits, passes, errors, "action-error")

                except Exception as e:
                    errors += 1
                    msg = f"Unexpected action error: {type(e).__name__}: {e}"
                    log_error(log_path, src, msg)
                    try:
                        cache_upsert(
                            cache_conn,
                            path=str(src), size=size, mtime_ns=mtime_ns,
                            status="error", detail=msg, action_mode=action_mode
                        )
                        cache_dirty_ops += 1
                    except Exception as ce:
                        print_event_line(f"[ERROR] cache write failed for {src} | {ce}")
                    csv_append_row(csv_path, [
                        datetime.now().isoformat(timespec="seconds"),
                        str(src), "error", "", "", "", action_mode, "", msg
                    ])
                    print_event_line(f"[ERROR] {src} | {msg}")
                    refresh_status_line(idx, len(to_scan), start_time, hits, passes, errors, "action-error")

            now = time.time()
            if cache_dirty_ops >= CACHE_FLUSH_EVERY_N or (now - last_cache_flush) >= CACHE_FLUSH_EVERY_SECONDS:
                flush_cache(cache_conn)
                cache_dirty_ops = 0
                last_cache_flush = now

            if (
                (idx - last_checkpoint_processed) >= CHECKPOINT_EVERY_FILES
                or (hits - last_checkpoint_hits) >= CHECKPOINT_EVERY_HITS
            ):
                elapsed = time.time() - start_time
                try:
                    free_usage = get_disk_usage(scan_root)
                    free_text = f"{format_bytes(free_usage.free)} / {format_bytes(free_usage.total)}"
                except Exception:
                    free_text = "unavailable"
                log_checkpoint(
                    log_path=log_path,
                    processed=idx,
                    planned=len(to_scan),
                    hits=hits,
                    passes=passes,
                    errors=errors,
                    free_space_text=free_text,
                    elapsed=format_seconds(elapsed),
                )
                print_event_line(
                    f"[CHECKPOINT] processed={idx:,}/{len(to_scan):,} | hits={hits:,} | pass={passes:,} | err={errors:,}"
                )
                refresh_status_line(idx, len(to_scan), start_time, hits, passes, errors, last_label)
                last_checkpoint_processed = idx
                last_checkpoint_hits = hits

        if pool is not None:
            pool.close()
            pool.join()

    except KeyboardInterrupt:
        stop_reason = "Stopped by user (Ctrl+C)."
        final_run_status = RUN_STATUS_INTERRUPTED
        print_event_line(f"[STOP] {stop_reason}")
        if pool is not None:
            try:
                pool.terminate()
                pool.join()
            except Exception:
                pass

    except Exception as e:
        stop_reason = f"Fatal runtime error: {type(e).__name__}: {e}"
        final_run_status = RUN_STATUS_FAILED
        print_event_line(f"[STOP] {stop_reason}")
        safe_log_append(log_path, "\nFATAL:\n" + traceback.format_exc() + "\n")
        if pool is not None:
            try:
                pool.terminate()
                pool.join()
            except Exception:
                pass

    finally:
        clear_status_line()
        try:
            flush_cache(cache_conn)
        except Exception:
            pass
        try:
            finish_run_history(
                cache_conn,
                run_id=run_id,
                status=final_run_status,
                stop_reason=stop_reason,
                hits=hits,
                passes=passes,
                errors=errors,
                actioned=actioned,
            )
        except Exception as e:
            safe_print(f"[WARN] Failed to finalize run history: {e}")
        close_cache(cache_conn)

    elapsed = time.time() - start_time

    try:
        quarantine_total_size = get_folder_size_bytes(quarantine_root)
    except Exception:
        quarantine_total_size = 0

    def safe_bucket_size(bucket_name: str) -> int:
        try:
            return get_folder_size_bytes(quarantine_root / bucket_name)
        except Exception:
            return 0

    black_size = safe_bucket_size(BUCKET_BLACK)
    docs_size = safe_bucket_size(BUCKET_DOCS)
    review_high_size = safe_bucket_size(BUCKET_REVIEW_HIGH)
    review_low_size = safe_bucket_size(BUCKET_REVIEW_LOW)

    try:
        final_usage = get_disk_usage(scan_root)
        final_free_text = f"{format_bytes(final_usage.free)} / {format_bytes(final_usage.total)}"
    except Exception:
        final_free_text = "Unavailable"

    safe_print()
    safe_print("=" * 72)
    safe_print("Finished")
    safe_print("=" * 72)

    if stop_reason:
        safe_print(f"Stop reason:               {stop_reason}")

    safe_print(f"Files in total scope:      {total_seen:,}")
    safe_print(f"Files actually scanned:    {len(to_scan):,}")
    safe_print(f"Skipped cached pass:       {skipped_cached_pass:,}")
    safe_print(f"Skipped cached hit:        {skipped_cached_hit:,}")
    safe_print(f"Rescanned missing hit:     {skipped_missing_quarantine:,}")
    safe_print(f"Retried cached errors:     {retrying_cached_error:,}")
    safe_print()
    safe_print(f"Elapsed time:              {format_seconds(elapsed)}")
    safe_print(f"Total hits:                {hits:,} ({ratio_str(hits, len(to_scan))} of scanned)")
    safe_print(f"  {BUCKET_BLACK:<24} {black_count:,}")
    safe_print(f"  {BUCKET_DOCS:<24} {docs_count:,}")
    safe_print(f"  {BUCKET_REVIEW_HIGH:<24} {review_high_count:,}")
    safe_print(f"  {BUCKET_REVIEW_LOW:<24} {review_low_count:,}")
    safe_print(f"Total pass files:          {passes:,} ({ratio_str(passes, len(to_scan))} of scanned)")
    safe_print(f"Errors:                    {errors:,} ({ratio_str(errors, len(to_scan))} of scanned)")
    safe_print(f"Files actioned:            {actioned:,}")
    safe_print(f"Disabled-bucket passes:    {disabled_bucket_hits:,}")
    safe_print(f"Dry-run hits:              {dry_run_hits:,}")
    safe_print()
    safe_print("Quarantine sizes:")
    safe_print(f"  Total quarantine size:   {format_bytes(quarantine_total_size)}")
    safe_print(f"  {BUCKET_BLACK:<24} {format_bytes(black_size)}")
    safe_print(f"  {BUCKET_DOCS:<24} {format_bytes(docs_size)}")
    safe_print(f"  {BUCKET_REVIEW_HIGH:<24} {format_bytes(review_high_size)}")
    safe_print(f"  {BUCKET_REVIEW_LOW:<24} {format_bytes(review_low_size)}")
    safe_print()
    safe_print(f"Remaining free space:      {final_free_text}")
    safe_print(f"Quarantine root:           {quarantine_root}")
    safe_print(f"Log file:                  {log_path}")
    safe_print(f"Resume cache DB:           {db_path}")
    safe_print(f"CSV report:                {csv_path}")
    safe_print()
    safe_print("Review quarantine manually before deleting anything.")

    maybe_open_quarantine_folder(quarantine_root)


# ============================================================
# MAIN
# ============================================================

def main():
    safe_print("=" * 72)
    safe_print("PDF Image Quarantine Sorter - guarded cached multiprocess edition")
    safe_print("=" * 72)

    try:
        cwd_usage = get_disk_usage(Path.cwd())
        safe_print(
            f"Current drive free space: "
            f"{format_bytes(cwd_usage.free)} / {format_bytes(cwd_usage.total)}"
        )
    except Exception as e:
        safe_print(f"Could not read current drive usage: {e}")

    mode = prompt_main_mode()

    if mode == "scan":
        scan_root = prompt_path()
        do_scan_mode(scan_root)
    else:
        scan_root = prompt_path()
        do_cache_maintenance(scan_root)


if __name__ == "__main__":
    main()
