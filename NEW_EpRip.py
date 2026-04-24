#!/usr/bin/env python3
#
#     Prizm presents
#   ▄████████    ▄███████▄    ▄████████  ▄█     ▄███████▄
#  ███    ███   ███    ███   ███    ███ ███    ███    ███
#  ███    █▀    ███    ███   ███    ███ ███▌   ███    ███
# ▄███▄▄▄       ███    ███  ▄███▄▄▄▄██▀ ███▌   ███    ███
#▀▀███▀▀▀      ▀█████████▀  ▀███▀▀▀▀▀   ███▌ ▀█████████▀
#  ███    █▄    ███        ▀███████████ ███    ███
#  ███    ███   ███ STEIN  - ███    ███ ███    ███ PER
#  ██████████  ▄████▀        ███    ███ █▀    ▄████▀
#   [ AUTOMATIC ]                       ███    ███ version 3.5
#                        A Prizmatik Underground Production
# Epstein DOJ Dataset Tools
# Author: Prizm (Prizmatik Underground)
# Repository:
# https://github.com/prizmatik666/epstein-ripper
# ====================================================#


import os
import re
import json
import time
import hashlib
import argparse
import asyncio
import sys
import random
import fcntl
import ast
import sqlite3
from contextlib import contextmanager
from urllib.parse import urljoin, urlparse
from datetime import datetime
from typing import Dict, Any, List, Tuple, Optional

import aiohttp
from playwright.async_api import async_playwright
from playwright.async_api import TimeoutError as PWTimeoutError
from playwright._impl._errors import TargetClosedError

try:
    import fitz  # PyMuPDF
    PYMUPDF_AVAILABLE = True
except Exception:
    fitz = None
    PYMUPDF_AVAILABLE = False

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

ANSI_RESET = "\033[0m"
ANSI_COLORS = {
    "red": "\033[91m",
    "green": "\033[92m",
    "white": "\033[97m",
    "orange": "\033[38;5;208m",
    "yellow": "\033[93m",
    "blue": "\033[94m",
    "pink": "\033[95m",
}
COLOR_ENABLED = sys.stdout.isatty()

# Throttling
SLEEP_BETWEEN_DOWNLOADS = 0.05
SLEEP_BETWEEN_PAGES = 0.5
SCAN_CHECKPOINT_EVERY_PAGES = 25

# Stop conditions
MAX_PAGES_WITH_NO_NEW_PDFS = 300
MAX_PAGES_HARD_CAP = 200000

# Retry behavior (for real download failures; poison has its own counters)
MAX_DOWNLOAD_RETRIES = 3
MAX_SCAN_PAGE_FETCH_RETRIES = 3
MAX_SCAN_PAGE_HARD_FAILURES = 8
SCAN_PAGE_HARD_FAILURE_COOLDOWN = 20.0
MAX_SCAN_AUTH_REFRESH_RETRIES = 3
SCAN_AUTH_REFRESH_COOLDOWN = 20.0

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
RETRY_BACKOFF_CAP = .7
RETRY_BACKOFF_JITTER = 0.35

# =========================================

SESSION = {
    "start_ts": datetime.now().isoformat(timespec="seconds"),
    "pages_scanned": 0,
    "new_pdfs_found": 0,
    "scan_page_fetch_failures": 0,
    "scan_auth_refreshes": 0,
    "scan_stops_on_error": 0,
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
        "pages_scanned": 0,
        "new_pdfs_found": 0,
        "scan_page_fetch_failures": 0,
        "scan_auth_refreshes": 0,
        "scan_stops_on_error": 0,
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
    lines.append(f"Pages scanned:                 {SESSION['pages_scanned']}")
    lines.append(f"New PDFs found:                {SESSION['new_pdfs_found']}")
    lines.append(f"Scan page fetch failures:      {SESSION['scan_page_fetch_failures']}")
    lines.append(f"Scan auth refreshes:           {SESSION['scan_auth_refreshes']}")
    lines.append(f"Scan stops on error:           {SESSION['scan_stops_on_error']}")
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
                f"  DS{ds}: pages={s['pages_scanned']} new={s['new_pdfs_found']} "
                f"fetch_fail={s['scan_page_fetch_failures']} refresh={s['scan_auth_refreshes']} "
                f"stop={s['scan_stops_on_error']} ok={s['downloaded_ok']} exist={s['marked_downloaded_existing']} "
                f"bad={s['skipped_bad_server_file']} poison={s['skipped_poison_hit_limit']} "
                f"refresh_limit={s['skipped_poison_refresh_limit']} max={s['skipped_max_retries']} "
                f"net={s['retryable_net_errors']} http={s['http_errors']} other={s['other_errors']}"
            )

    lines.append("=" * 78)

    # Print to terminal
    if COLOR_ENABLED:
        rendered_lines = []
        for line in lines:
            if line.startswith("=") or line.startswith("-"):
                rendered_lines.append(ansi(line, "red"))
            elif line.startswith("SESSION SUMMARY"):
                rendered_lines.append(
                    ansi_with_red_numbers(line.replace("SESSION SUMMARY", "__SESSION_SUMMARY__"), "green")
                    .replace("__SESSION_SUMMARY__", ansi("SESSION SUMMARY", "orange"))
                )
            elif line.startswith("  DS"):
                rendered_lines.append(colorize_log_message(line))
            else:
                rendered_lines.append(ansi_with_red_numbers(line, "white"))
        print("\n" + "\n".join(rendered_lines) + "\n")
    else:
        print("\n" + "\n".join(lines) + "\n")

    # Also append to download.log (without timestamps; it's a summary block)
    _append_to_logfile([""] + lines + [""])


def install_loop_exception_handler() -> None:
    loop = asyncio.get_running_loop()
    default_handler = loop.get_exception_handler()

    def _handler(loop, context):
        exc = context.get("exception")
        msg = context.get("message", "")

        if isinstance(exc, TargetClosedError):
            return
        if "Target page, context or browser has been closed" in msg:
            return
        if exc is not None and "Target page, context or browser has been closed" in str(exc):
            return

        if default_handler is not None:
            default_handler(loop, context)
        else:
            loop.default_exception_handler(context)

    loop.set_exception_handler(_handler)


def ansi(text: str, color: str) -> str:
    if not COLOR_ENABLED:
        return text
    return f"{ANSI_COLORS[color]}{text}{ANSI_RESET}"


def colorize_numbers(text: str) -> str:
    if not COLOR_ENABLED:
        return text
    return re.sub(r"(\d+|\*)", lambda m: ansi(m.group(1), "red"), text)


def ansi_with_red_numbers(text: str, base_color: str) -> str:
    if not COLOR_ENABLED:
        return text
    base = ANSI_COLORS[base_color]
    red = ANSI_COLORS["red"]
    return base + re.sub(r"(\d+|\*)", lambda m: f"{red}{m.group(1)}{base}", text) + ANSI_RESET


def ui_text(text: str, color: str = "white", numbers_red: bool = False) -> str:
    if numbers_red:
        return ansi_with_red_numbers(text, color)
    return ansi(text, color)


def ui_option(label: str, description: str) -> str:
    return (
        "  "
        f"{ansi(label, 'orange')}"
        f" {ansi('=', 'red')} "
        f"{ui_text(description, 'white', numbers_red=True)}"
    )


def format_dataset_list(values: List[int]) -> str:
    parts = []
    for i, value in enumerate(values):
        if i:
            parts.append(ansi(",", "white"))
        parts.append(ansi(str(value), "red"))
    return "".join(parts)


def colorize_log_message(msg: str) -> str:
    if not COLOR_ENABLED:
        return msg

    if msg.startswith("Mode: "):
        return ansi("Mode:", "orange") + " " + ansi(msg[len("Mode: "):], "orange")

    if msg.startswith("Scan start options: "):
        raw = msg[len("Scan start options: "):]
        try:
            data = ast.literal_eval(raw)
        except Exception:
            return ansi("Scan start options:", "orange") + " " + ansi_with_red_numbers(raw, "white")

        if isinstance(data, dict):
            parts = []
            for i, (key, value) in enumerate(data.items()):
                if i:
                    parts.append(ansi(", ", "white"))
                parts.append(ansi(repr(key), "white"))
                parts.append(ansi(": ", "white"))
                if isinstance(value, (int, float)):
                    parts.append(ansi(str(value), "red"))
                elif value is None:
                    parts.append(ansi("None", "orange"))
                else:
                    parts.append(ansi(repr(value), "orange"))
            return (
                ansi("Scan start options:", "orange") + " "
                + ansi("{", "green")
                + "".join(parts)
                + ansi("}", "green")
            )

    m = re.match(r"^(===) DATASET (\d+) START \((mode=[^)]+)\) (===)$", msg)
    if m:
        return (
            f"{ansi(m.group(1), 'blue')} "
            f"{ansi('DATASET', 'white')} {ansi(m.group(2), 'red')} "
            f"{ansi('START', 'white')} "
            f"{ansi(f'({m.group(3)})', 'orange')} "
            f"{ansi(m.group(4), 'blue')}"
        )

    m = re.match(r"^(===) DATASET (\d+) COMPLETE (===)$", msg)
    if m:
        return (
            f"{ansi(m.group(1), 'blue')} "
            f"{ansi('DATASET', 'white')} {ansi(m.group(2), 'red')} "
            f"{ansi('COMPLETE', 'white')} "
            f"{ansi(m.group(3), 'blue')}"
        )

    m = re.match(r"^(.*\]) DOWNLOAD (.+)$", msg)
    if m:
        prefix = colorize_log_message(m.group(1)) if m.group(1) != msg else ansi_with_red_numbers(m.group(1), "white")
        return f"{prefix} {ansi('DOWNLOAD', 'orange')} {ansi(m.group(2), 'white')}"

    m = re.match(r"^(.*\]) DONE \((\d+)\) (.+)$", msg)
    if m:
        prefix = colorize_log_message(m.group(1)) if m.group(1) != msg else ansi_with_red_numbers(m.group(1), "white")
        return (
            f"{prefix} {ansi('DONE', 'green')} "
            f"{ansi('(', 'white')}{ansi(m.group(2), 'red')}{ansi(')', 'white')} "
            f"{ansi(m.group(3), 'white')}"
        )

    m = re.match(r"^(.*\]) Page (\d+) immediate download pass complete ΓÇö (\d+) new PDFs$", msg)
    if m:
        prefix = colorize_log_message(m.group(1)) if m.group(1) != msg else ansi_with_red_numbers(m.group(1), "white")
        return (
            f"{prefix} {ansi('Page', 'white')} {ansi(m.group(2), 'red')} "
            f"{ansi('immediate download pass complete', 'orange')} {ansi('ΓÇö', 'white')} "
            f"{ansi(m.group(3), 'red')} {ansi('new PDFs', 'pink')}"
        )

    m = re.match(r"^(.*\]) Download pass complete ΓÇö (\d+) new PDFs$", msg)
    if m:
        prefix = colorize_log_message(m.group(1)) if m.group(1) != msg else ansi_with_red_numbers(m.group(1), "white")
        return (
            f"{prefix} {ansi('Download pass complete', 'orange')} {ansi('ΓÇö', 'white')} "
            f"{ansi(m.group(2), 'red')} {ansi('new PDFs', 'pink')}"
        )

    placeholders: Dict[str, str] = {}

    def stash(pattern: str, render_fn):
        nonlocal msg

        def repl(m):
            key = f"__TOK_{chr(65 + len(placeholders))}__"
            placeholders[key] = render_fn(m)
            return key

        msg = re.sub(pattern, repl, msg)

    stash(r"\[DS (\d+)\]", lambda m: ansi(f"[DS {m.group(1)}]", "orange"))
    stash(r"\[scan\]", lambda m: ansi("[scan]", "orange"))
    stash(r"\[auth\]", lambda m: ansi("[auth]", "yellow"))
    stash(r"DS=(\d+)", lambda m: f"{ansi('DS=', 'white')}{ansi(m.group(1), 'orange')}")
    stash(r"\(mode=[^)]+\)", lambda m: ansi(m.group(0), "orange"))
    stash(r"No NEW PDFs", lambda m: ansi(m.group(0), "blue"))
    stash(r"NEW PDFs", lambda m: ansi(m.group(0), "pink"))

    msg = ansi_with_red_numbers(msg, "white")

    for key, value in placeholders.items():
        msg = msg.replace(key, value)

    return msg


def render_log_line(ts: str, msg: str) -> str:
    if not COLOR_ENABLED:
        return f"[{ts}] {msg}"
    return f"{ansi(f'[{ts}]', 'green')} {colorize_log_message(msg)}"


def log(msg: str) -> None:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(render_log_line(ts, msg))
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
    print(render_log_line(ts, line.split("] ", 1)[1]))
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


def _lock_path_for(path: str) -> str:
    return path + ".lock"


@contextmanager
def locked_file(path: str, exclusive: bool):
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    lock_path = _lock_path_for(path)
    with open(lock_path, "a+", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
        try:
            yield lock_file
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _json_retry_message(path: str, attempt: int, max_attempts: int, cooldown_seconds: int) -> None:
    log(f"ERROR reading {path}..retrying (attempt {attempt}/{max_attempts})")
    log(f"Cooldown {cooldown_seconds}sec")


def _load_json_unlocked(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise RuntimeError(f"Top-level JSON object expected in {path}")
    return data


def ensure_index_runtime_tracking(idx: Dict[str, Any]) -> Dict[str, Any]:
    dirty_files = idx.get("__dirty_files__")
    if not isinstance(dirty_files, set):
        idx["__dirty_files__"] = set(dirty_files or [])
    idx["__meta_dirty__"] = bool(idx.get("__meta_dirty__", False))
    idx["__force_full_save__"] = bool(idx.get("__force_full_save__", False))
    return idx


def persistable_index_data(idx: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "meta": dict(idx.get("meta", {})),
        "files": dict(idx.get("files", {})),
    }


def mark_index_entry_dirty(idx: Dict[str, Any], filename: str) -> None:
    ensure_index_runtime_tracking(idx)
    idx["__dirty_files__"].add(filename)


def mark_index_meta_dirty(idx: Dict[str, Any]) -> None:
    ensure_index_runtime_tracking(idx)
    idx["__meta_dirty__"] = True


def mark_index_force_full_save(idx: Dict[str, Any]) -> None:
    ensure_index_runtime_tracking(idx)
    idx["__force_full_save__"] = True


def clear_index_dirty_tracking(idx: Dict[str, Any]) -> None:
    ensure_index_runtime_tracking(idx)
    idx["__dirty_files__"].clear()
    idx["__meta_dirty__"] = False
    idx["__force_full_save__"] = False


def _load_sqlite_unlocked(path: str) -> Dict[str, Any]:
    conn = sqlite3.connect(path)
    try:
        cur = conn.cursor()
        tables = {row[0] for row in cur.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        if "meta" not in tables or "files" not in tables:
            raise RuntimeError(f"Expected meta/files tables in {path}")

        meta: Dict[str, Any] = {}
        for key, value_json in cur.execute("SELECT key, value_json FROM meta"):
            try:
                meta[key] = json.loads(value_json)
            except Exception:
                meta[key] = value_json

        files: Dict[str, Any] = {}
        rows = cur.execute(
            """
            SELECT filename, raw_json, url, page, downloaded, downloaded_at, bytes,
                   sha256, attempts, skipped, skip_reason, last_error,
                   poison_hits, poison_refreshes, first_seen, last_seen
            FROM files
            """
        )
        for row in rows:
            (
                filename,
                raw_json,
                url,
                page,
                downloaded,
                downloaded_at,
                num_bytes,
                sha256,
                attempts,
                skipped,
                skip_reason,
                last_error,
                poison_hits,
                poison_refreshes,
                first_seen,
                last_seen,
            ) = row
            entry: Dict[str, Any]
            if raw_json:
                try:
                    parsed = json.loads(raw_json)
                    entry = parsed if isinstance(parsed, dict) else {}
                except Exception:
                    entry = {}
            else:
                entry = {}

            entry.setdefault("url", url)
            entry.setdefault("page", page)
            entry.setdefault("downloaded", bool(downloaded))
            entry.setdefault("downloaded_at", downloaded_at)
            entry.setdefault("bytes", num_bytes)
            entry.setdefault("sha256", sha256)
            entry.setdefault("attempts", attempts or 0)
            entry.setdefault("skipped", bool(skipped))
            entry.setdefault("skip_reason", skip_reason)
            entry.setdefault("last_error", last_error)
            entry.setdefault("poison_hits", poison_hits or 0)
            entry.setdefault("poison_refreshes", poison_refreshes or 0)
            entry.setdefault("first_seen", first_seen)
            entry.setdefault("last_seen", last_seen)
            files[filename] = entry

        return ensure_index_runtime_tracking({"meta": meta, "files": files})
    finally:
        conn.close()


def _ensure_sqlite_schema(conn: sqlite3.Connection) -> None:
    cur = conn.cursor()
    cur.execute("PRAGMA journal_mode=WAL")
    cur.execute("PRAGMA synchronous=NORMAL")
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS meta (
            key TEXT PRIMARY KEY,
            value_json TEXT NOT NULL
        )
        """
    )
    cur.execute(
        """
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
        )
        """
    )
    cur.execute("CREATE INDEX IF NOT EXISTS idx_files_downloaded ON files(downloaded)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_files_page ON files(page)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_files_skipped ON files(skipped)")


def _write_sqlite_unlocked(path: str, data: Dict[str, Any]) -> None:
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)

    tmp = path + ".tmp"
    if os.path.exists(tmp):
        os.remove(tmp)

    conn = sqlite3.connect(tmp)
    try:
        cur = conn.cursor()
        cur.execute("PRAGMA journal_mode=DELETE")
        cur.execute("PRAGMA synchronous=FULL")
        _ensure_sqlite_schema(conn)

        meta = data.get("meta", {})
        cur.executemany(
            "INSERT INTO meta(key, value_json) VALUES (?, ?)",
            [(key, json.dumps(value, sort_keys=True)) for key, value in meta.items()],
        )

        rows = []
        for filename, entry in data.get("files", {}).items():
            rows.append(
                (
                    filename,
                    entry.get("url"),
                    entry.get("page"),
                    1 if entry.get("downloaded") else 0,
                    entry.get("downloaded_at"),
                    entry.get("bytes"),
                    entry.get("sha256"),
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
            )
            if len(rows) >= 10000:
                cur.executemany(
                    """
                    INSERT INTO files(
                        filename, url, page, downloaded, downloaded_at, bytes, sha256,
                        attempts, skipped, skip_reason, last_error, poison_hits,
                        poison_refreshes, first_seen, last_seen, raw_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    rows,
                )
                rows.clear()
        if rows:
            cur.executemany(
                """
                INSERT INTO files(
                    filename, url, page, downloaded, downloaded_at, bytes, sha256,
                    attempts, skipped, skip_reason, last_error, poison_hits,
                    poison_refreshes, first_seen, last_seen, raw_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )

        conn.commit()
    finally:
        conn.close()

    os.replace(tmp, path)


def _fetch_sqlite_meta(conn: sqlite3.Connection) -> Dict[str, Any]:
    meta: Dict[str, Any] = {}
    for key, value_json in conn.execute("SELECT key, value_json FROM meta"):
        try:
            meta[key] = json.loads(value_json)
        except Exception:
            meta[key] = value_json
    return meta


def _fetch_sqlite_entries(conn: sqlite3.Connection, filenames: List[str]) -> Dict[str, Dict[str, Any]]:
    if not filenames:
        return {}

    placeholders = ",".join("?" for _ in filenames)
    rows = conn.execute(
        f"""
        SELECT filename, raw_json, url, page, downloaded, downloaded_at, bytes,
               sha256, attempts, skipped, skip_reason, last_error,
               poison_hits, poison_refreshes, first_seen, last_seen
        FROM files
        WHERE filename IN ({placeholders})
        """,
        filenames,
    ).fetchall()

    existing: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        (
            filename,
            raw_json,
            url,
            page,
            downloaded,
            downloaded_at,
            num_bytes,
            sha256,
            attempts,
            skipped,
            skip_reason,
            last_error,
            poison_hits,
            poison_refreshes,
            first_seen,
            last_seen,
        ) = row
        try:
            entry = json.loads(raw_json) if raw_json else {}
            if not isinstance(entry, dict):
                entry = {}
        except Exception:
            entry = {}
        entry.setdefault("url", url)
        entry.setdefault("page", page)
        entry.setdefault("downloaded", bool(downloaded))
        entry.setdefault("downloaded_at", downloaded_at)
        entry.setdefault("bytes", num_bytes)
        entry.setdefault("sha256", sha256)
        entry.setdefault("attempts", attempts or 0)
        entry.setdefault("skipped", bool(skipped))
        entry.setdefault("skip_reason", skip_reason)
        entry.setdefault("last_error", last_error)
        entry.setdefault("poison_hits", poison_hits or 0)
        entry.setdefault("poison_refreshes", poison_refreshes or 0)
        entry.setdefault("first_seen", first_seen)
        entry.setdefault("last_seen", last_seen)
        existing[filename] = entry

    return existing


def _upsert_sqlite_entries(conn: sqlite3.Connection, rows: List[Tuple[Any, ...]]) -> None:
    if not rows:
        return
    conn.executemany(
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
        rows,
    )


def _merge_meta(existing: Dict[str, Any], incoming: Dict[str, Any]) -> Dict[str, Any]:
    merged = dict(existing)

    def _prefer_earliest(a, b):
        vals = [v for v in (a, b) if v]
        return min(vals) if vals else None

    def _prefer_latest(a, b):
        vals = [v for v in (a, b) if v]
        return max(vals) if vals else None

    merged["dataset"] = incoming.get("dataset", existing.get("dataset"))
    merged["version"] = max(int(existing.get("version", 0) or 0), int(incoming.get("version", 0) or 0))
    merged["created_at"] = _prefer_earliest(existing.get("created_at"), incoming.get("created_at"))
    merged["last_scan_at"] = _prefer_latest(existing.get("last_scan_at"), incoming.get("last_scan_at"))
    merged["last_scan_page"] = max(
        int(existing.get("last_scan_page", 0) or 0),
        int(incoming.get("last_scan_page", 0) or 0),
    )

    for key in set(existing.keys()) | set(incoming.keys()):
        if key in {"dataset", "version", "created_at", "last_scan_at", "last_scan_page"}:
            continue
        merged[key] = incoming.get(key, existing.get(key))

    return merged


def _merge_index_entry(existing: Dict[str, Any], incoming: Dict[str, Any]) -> Dict[str, Any]:
    merged = dict(existing)

    def _prefer_earliest(a, b):
        vals = [v for v in (a, b) if v]
        return min(vals) if vals else None

    def _prefer_latest(a, b):
        vals = [v for v in (a, b) if v]
        return max(vals) if vals else None

    merged["url"] = incoming.get("url") or existing.get("url")
    merged["first_seen"] = _prefer_earliest(existing.get("first_seen"), incoming.get("first_seen"))
    merged["last_seen"] = _prefer_latest(existing.get("last_seen"), incoming.get("last_seen"))
    merged["page"] = incoming.get("page", existing.get("page"))
    merged["downloaded"] = bool(existing.get("downloaded")) or bool(incoming.get("downloaded"))

    if merged["downloaded"]:
        merged["downloaded_at"] = _prefer_earliest(existing.get("downloaded_at"), incoming.get("downloaded_at"))
    else:
        merged["downloaded_at"] = incoming.get("downloaded_at", existing.get("downloaded_at"))

    existing_bytes = existing.get("bytes")
    incoming_bytes = incoming.get("bytes")
    if existing_bytes is None:
        merged["bytes"] = incoming_bytes
    elif incoming_bytes is None:
        merged["bytes"] = existing_bytes
    else:
        merged["bytes"] = max(existing_bytes, incoming_bytes)

    merged["sha256"] = incoming.get("sha256") or existing.get("sha256")
    merged["attempts"] = max(int(existing.get("attempts", 0) or 0), int(incoming.get("attempts", 0) or 0))
    merged["last_error"] = incoming.get("last_error") if incoming.get("last_error") is not None else existing.get("last_error")
    merged["poison_hits"] = max(int(existing.get("poison_hits", 0) or 0), int(incoming.get("poison_hits", 0) or 0))
    merged["poison_refreshes"] = max(
        int(existing.get("poison_refreshes", 0) or 0),
        int(incoming.get("poison_refreshes", 0) or 0),
    )
    merged["skipped"] = bool(existing.get("skipped")) or bool(incoming.get("skipped"))
    merged["skip_reason"] = incoming.get("skip_reason") or existing.get("skip_reason")

    for key in set(existing.keys()) | set(incoming.keys()):
        if key in merged:
            continue
        merged[key] = incoming.get(key, existing.get(key))

    return merged


def merge_index_data(existing: Dict[str, Any], incoming: Dict[str, Any]) -> Dict[str, Any]:
    if not existing:
        return incoming
    if not incoming:
        return existing

    existing_files = existing.get("files", {})
    incoming_files = incoming.get("files", {})
    if not isinstance(existing_files, dict) or not isinstance(incoming_files, dict):
        return incoming

    merged = {
        "meta": _merge_meta(existing.get("meta", {}), incoming.get("meta", {})),
        "files": {},
    }

    for filename in set(existing_files.keys()) | set(incoming_files.keys()):
        existing_entry = existing_files.get(filename)
        incoming_entry = incoming_files.get(filename)

        if isinstance(existing_entry, dict) and isinstance(incoming_entry, dict):
            merged["files"][filename] = _merge_index_entry(existing_entry, incoming_entry)
        elif isinstance(incoming_entry, dict):
            merged["files"][filename] = incoming_entry
        elif isinstance(existing_entry, dict):
            merged["files"][filename] = existing_entry

    return merged


def safe_json_load(path: str) -> Dict[str, Any]:
    if not os.path.exists(path):
        return {}
    max_attempts = 3
    cooldown_seconds = 20

    for attempt in range(1, max_attempts + 1):
        try:
            with locked_file(path, exclusive=False):
                return _load_json_unlocked(path)
        except Exception as e:
            if attempt < max_attempts:
                _json_retry_message(path, attempt, max_attempts, cooldown_seconds)
                time.sleep(cooldown_seconds)
                continue

            msg = (
                f"ERROR reading {path} after {max_attempts} attempts. "
                f"Index load strikeout reached. Please restart the program. "
                f"Last error: {repr(e)}"
            )
            log(msg)
            raise RuntimeError(msg) from e


def safe_json_save(path: str, data: Dict[str, Any]) -> None:
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)

    tmp = path + ".tmp"

    with locked_file(path, exclusive=True):
        latest: Dict[str, Any] = {}
        if os.path.exists(path):
            latest = _load_json_unlocked(path)
        merged = merge_index_data(latest, data)

        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(merged, f, indent=2, sort_keys=True)
            f.flush()
            try:
                os.fsync(f.fileno())
            except Exception:
                pass

        for attempt in range(3):
            try:
                os.replace(tmp, path)
                data.clear()
                data.update(merged)
                return
            except FileNotFoundError:
                if attempt < 2:
                    with open(tmp, "w", encoding="utf-8") as f:
                        json.dump(merged, f, indent=2, sort_keys=True)
                        f.flush()
                        try:
                            os.fsync(f.fileno())
                        except Exception:
                            pass
                    time.sleep(0.05)
                    continue
                raise


def load_index_data(path: str) -> Dict[str, Any]:
    if not os.path.exists(path):
        return {}
    if path.lower().endswith(".sqlite"):
        max_attempts = 3
        cooldown_seconds = 20
        for attempt in range(1, max_attempts + 1):
            try:
                with locked_file(path, exclusive=False):
                    return _load_sqlite_unlocked(path)
            except Exception as e:
                if attempt < max_attempts:
                    _json_retry_message(path, attempt, max_attempts, cooldown_seconds)
                    time.sleep(cooldown_seconds)
                    continue
                msg = (
                    f"ERROR reading {path} after {max_attempts} attempts. "
                    f"Index load strikeout reached. Please restart the program. "
                    f"Last error: {repr(e)}"
                )
                log(msg)
                raise RuntimeError(msg) from e
    return safe_json_load(path)


def save_index_data(path: str, data: Dict[str, Any]) -> None:
    ensure_index_runtime_tracking(data)
    persistable = persistable_index_data(data)

    if path.lower().endswith(".sqlite"):
        with locked_file(path, exclusive=True):
            force_full = bool(data.get("__force_full_save__")) or not os.path.exists(path)
            dirty_files = sorted(data.get("__dirty_files__", set()))
            meta_dirty = bool(data.get("__meta_dirty__", False))

            if force_full:
                _write_sqlite_unlocked(path, persistable)
            else:
                conn = sqlite3.connect(path)
                try:
                    _ensure_sqlite_schema(conn)

                    if meta_dirty:
                        existing_meta = _fetch_sqlite_meta(conn)
                        merged_meta = _merge_meta(existing_meta, persistable["meta"])
                        conn.executemany(
                            """
                            INSERT INTO meta(key, value_json) VALUES (?, ?)
                            ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json
                            """,
                            [(key, json.dumps(value, sort_keys=True)) for key, value in merged_meta.items()],
                        )
                        persistable["meta"] = merged_meta
                        data["meta"] = dict(merged_meta)

                    if dirty_files:
                        latest_entries = _fetch_sqlite_entries(conn, dirty_files)
                        rows = []
                        for filename in dirty_files:
                            incoming_entry = persistable["files"].get(filename)
                            if not isinstance(incoming_entry, dict):
                                continue
                            latest_entry = latest_entries.get(filename)
                            merged_entry = _merge_index_entry(latest_entry, incoming_entry) if latest_entry else incoming_entry
                            persistable["files"][filename] = merged_entry
                            data["files"][filename] = merged_entry
                            rows.append(
                                (
                                    filename,
                                    merged_entry.get("url"),
                                    merged_entry.get("page"),
                                    1 if merged_entry.get("downloaded") else 0,
                                    merged_entry.get("downloaded_at"),
                                    merged_entry.get("bytes"),
                                    merged_entry.get("sha256"),
                                    int(merged_entry.get("attempts", 0) or 0),
                                    1 if merged_entry.get("skipped") else 0,
                                    merged_entry.get("skip_reason"),
                                    merged_entry.get("last_error"),
                                    int(merged_entry.get("poison_hits", 0) or 0),
                                    int(merged_entry.get("poison_refreshes", 0) or 0),
                                    merged_entry.get("first_seen"),
                                    merged_entry.get("last_seen"),
                                    json.dumps(merged_entry, sort_keys=True),
                                )
                            )
                        _upsert_sqlite_entries(conn, rows)

                    conn.commit()
                finally:
                    conn.close()
            clear_index_dirty_tracking(data)
        return
    safe_json_save(path, persistable)
    clear_index_dirty_tracking(data)


def load_resume_page(state_file: str) -> Optional[int]:
    if os.path.exists(state_file):
        try:
            with locked_file(state_file, exclusive=False):
                with open(state_file, "r", encoding="utf-8") as f:
                    n = int(f.read().strip())
                    return n if n >= 1 else None
        except Exception:
            return None
    return None


def save_resume_page(state_file: str, page_num: int) -> None:
    parent = os.path.dirname(state_file)
    if parent:
        os.makedirs(parent, exist_ok=True)

    tmp = state_file + ".tmp"
    with locked_file(state_file, exclusive=True):
        current = 0
        if os.path.exists(state_file):
            try:
                with open(state_file, "r", encoding="utf-8") as f:
                    current = int(f.read().strip() or "0")
            except Exception:
                current = 0
        target = max(current, page_num)
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(str(target))
            f.flush()
            try:
                os.fsync(f.fileno())
            except Exception:
                pass
        os.replace(tmp, state_file)


def ask_datasets_interactive() -> List[int]:
    available = sorted(DATASETS.keys())
    print("\n" + ansi("Available datasets:", "green"))
    print(format_dataset_list(available))

    raw = input(
        "\n" + ui_text(
            "Enter dataset numbers separated by commas (example: 1,3,5) "
            "or a range (example: 1-11): ",
            "white",
            numbers_red=True,
        )
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
        print(ui_text("No valid datasets selected. Exiting.", "white"))
        raise SystemExit(1)

    return sorted(selected)


def ask_mode_interactive() -> str:
    print("\n" + ui_text("Mode options:", "white"))
    print(ui_option("sync", "scan + download (recommended)"))
    print(ui_option("scan", "only scan and update index (no downloads)"))
    print(ui_option("download", "only download missing from index (no scanning)"))

    raw = input("\n" + ansi("Choose mode [sync]: ", "red")).strip().lower()
    return raw if raw in {"sync", "scan", "download"} else "sync"


def ask_scan_start_options_interactive() -> Dict[str, Any]:
    print("\n" + ui_text("Scan start options:", "white"))
    print(ui_option("continue", "continue from resume file / last scanned page (current behavior)"))
    print(ui_option("page1", "rescan from page 1"))
    print(ui_option("range", "scan an explicit page range"))
    print("             " + ui_text("tip: use 1-* for open-ended range scan until no-new-streak stop", "white", numbers_red=True))

    while True:
        raw = input("\n" + ansi("Choose scan start [continue]: ", "red")).strip().lower()
        choice = raw or "continue"

        if choice in {"continue", "c"}:
            return {"kind": "continue"}

        if choice in {"page1", "1", "restart", "rescan"}:
            return {"kind": "page1"}

        if choice in {"range", "r"}:
            range_raw = input(ansi("Enter page range (example: 1-250 or 1-*): ", "red")).strip()
            m = re.match(r"^\s*(\d+)\s*-\s*(\d+|\*)\s*$", range_raw)
            if not m:
                print(ui_text("Invalid range. Please enter it like 1-250 or 1-*.", "white", numbers_red=True))
                continue

            start_page = int(m.group(1))
            end_raw = m.group(2)
            if start_page < 1:
                print(ui_text("Pages must be 1 or greater.", "white", numbers_red=True))
                continue

            if end_raw == "*":
                end_page = None
            else:
                end_page = int(end_raw)
                if end_page < 1:
                    print(ui_text("Pages must be 1 or greater.", "white", numbers_red=True))
                    continue
                if start_page > end_page:
                    start_page, end_page = end_page, start_page

            return {
                "kind": "range",
                "start_page": start_page,
                "end_page": end_page,
            }

        print(ui_text("Invalid choice. Enter continue, page1, or range.", "white", numbers_red=True))


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


async def ensure_age_verified(
    page,
    dataset_id: int,
    *,
    click_if_found: bool = True,
    log_detection: bool = True,
) -> bool:
    try:
        gate = page.locator(AGE_GATE_BLOCK).first
        if await gate.is_visible(timeout=250):
            if log_detection:
                if click_if_found:
                    log(f"[DS {dataset_id}] [auth] Age gate detected -> clicking YES")
                else:
                    log(f"[DS {dataset_id}] [auth] Age gate detected")
            if click_if_found:
                await page.locator(AGE_YES_BTN).click(timeout=8000)
                await asyncio.sleep(AUTH_SLEEP_AFTER_AGE_CLICK)
            return True
    except PWTimeoutError:
        return False
    except Exception:
        return False
    return False


async def wait_for_dataset_list(
    page,
    dataset_id: int,
    timeout_s: int = AUTH_WAIT_SECONDS,
    *,
    click_age_gate: bool = True,
    log_age_gate_detection: bool = True,
) -> None:
    log(f"[DS {dataset_id}] [auth] Validating access and loading dataset list...")
    deadline = time.time() + timeout_s

    while True:
        await ensure_robot_verified(page, dataset_id=dataset_id)
        await ensure_age_verified(
            page,
            dataset_id=dataset_id,
            click_if_found=click_age_gate,
            log_detection=log_age_gate_detection,
        )

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


async def create_fresh_context(
    browser,
    first_page_url: str,
    dataset_id: int,
    *,
    click_age_gate: bool = True,
    log_age_gate_detection: bool = True,
):
    context = await browser.new_context()
    page = await context.new_page()

    log(f"[DS {dataset_id}] NEW CONTEXT ΓÇö starting DOJ session...")
    await page.goto(first_page_url, wait_until="domcontentloaded")
    await asyncio.sleep(AUTH_SLEEP_AFTER_GOTO)

    await ensure_robot_verified(page, dataset_id=dataset_id)
    await ensure_age_verified(
        page,
        dataset_id=dataset_id,
        click_if_found=click_age_gate,
        log_detection=log_age_gate_detection,
    )
    await wait_for_dataset_list(
        page,
        dataset_id=dataset_id,
        timeout_s=AUTH_WAIT_SECONDS,
        click_age_gate=click_age_gate,
        log_age_gate_detection=log_age_gate_detection,
    )

    if AUTH_SESSION_SETTLE_SECONDS and AUTH_SESSION_SETTLE_SECONDS > 0:
        log(f"[DS {dataset_id}] [auth] Session initialized ΓÇö settling ({AUTH_SESSION_SETTLE_SECONDS:.1f}s)")
        await asyncio.sleep(AUTH_SESSION_SETTLE_SECONDS)

    if KEEP_AUTH_PAGE_OPEN_SECONDS and KEEP_AUTH_PAGE_OPEN_SECONDS > 0:
        log(f"[DS {dataset_id}] [auth] Holding auth window open ({KEEP_AUTH_PAGE_OPEN_SECONDS:.1f}s)")
        await asyncio.sleep(KEEP_AUTH_PAGE_OPEN_SECONDS)

    if CLOSE_AUTH_PAGE_AFTER_AUTH:
        try:
            await page.close()
            page = None
            log(f"[DS {dataset_id}] [auth] Session ready ΓÇö proceeding to work queue")
        except Exception:
            pass

    return context, page


async def ensure_auth_browser(playwright, browser):
    if browser is not None and browser.is_connected():
        return browser
    return await playwright.chromium.launch(headless=False, slow_mo=25)


async def create_hidden_scan_worker(playwright, auth_context):
    worker_browser = await playwright.chromium.launch(headless=True)
    worker_context = await worker_browser.new_context(
        storage_state=await auth_context.storage_state()
    )
    worker_page = await worker_context.new_page()
    return worker_browser, worker_context, worker_page


async def close_hidden_scan_worker(worker_page, worker_context, worker_browser) -> None:
    if worker_page is not None:
        try:
            await worker_page.close()
        except Exception:
            pass
    if worker_context is not None:
        try:
            await worker_context.close()
        except Exception:
            pass
    if worker_browser is not None:
        try:
            await worker_browser.close()
        except Exception:
            pass


async def fetch_pdf(context, url: str, referer: str):
    return await context.request.get(
        url,
        timeout=180000,
        headers={
            "Referer": referer,
            "Accept": "application/pdf,*/*",
        },
    )


async def build_scan_request_context(playwright, browser_context):
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


async def fetch_scan_page_html(request_context, url: str, referer: str):
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
    raise RuntimeError(
        f"[DS {dataset_id}] [scan] Exhausted page fetch retries for page {page_num}: {repr(last_err)}"
    )


def is_valid_epstein_pdf_url(full_url: str) -> bool:
    u = full_url.lower()
    return ("/epstein/files/" in u) and u.endswith(".pdf")


def extract_file_num(filename: str) -> Optional[int]:
    m = re.match(r"EFTA0*(\d+)\.pdf$", filename, re.IGNORECASE)
    if not m:
        return None
    return int(m.group(1))


def html_has_scan_auth_gate(html: str) -> bool:
    if not html:
        return False
    lowered = html.lower().replace("\\/", "/")
    if "block-usdoj-external-files-block" in lowered or "/epstein/files/" in lowered:
        return False
    return (
        "access denied" in lowered or
        "403 forbidden" in lowered or
        "forbidden" in lowered
    )


def extract_scan_page_pdfs(html: str) -> List[Tuple[str, str]]:
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


async def extract_scan_page_pdfs_from_browser_page(
    page,
    dataset_id: int,
    page_url: str,
) -> List[Tuple[str, str]]:
    log(f"[DS {dataset_id}] [scan] Verifying page via browser context...")
    await page.goto(page_url, wait_until="domcontentloaded")
    try:
        await page.wait_for_load_state("networkidle", timeout=10000)
    except PWTimeoutError:
        pass
    hrefs = await page.eval_on_selector_all(
        "a[href]",
        "els => els.map(e => e.getAttribute('href'))"
    )
    pdfs: List[Tuple[str, str]] = []
    seen = set()
    for href in hrefs:
        if not href:
            continue
        full_url = urljoin(BASE_SITE, href)
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
    *,
    click_age_gate: bool,
    log_age_gate_detection: bool,
) -> None:
    page = await context.new_page()
    try:
        log(f"[DS {dataset_id}] [scan] Refreshing auth window...")
        await page.goto(first_page_url, wait_until="domcontentloaded")
        await asyncio.sleep(AUTH_SLEEP_AFTER_GOTO)
        await ensure_robot_verified(page, dataset_id=dataset_id)
        await ensure_age_verified(
            page,
            dataset_id=dataset_id,
            click_if_found=click_age_gate,
            log_detection=log_age_gate_detection,
        )
        await wait_for_dataset_list(
            page,
            dataset_id=dataset_id,
            timeout_s=AUTH_WAIT_SECONDS,
            click_age_gate=click_age_gate,
            log_age_gate_detection=log_age_gate_detection,
        )
        if AUTH_SESSION_SETTLE_SECONDS and AUTH_SESSION_SETTLE_SECONDS > 0:
            await asyncio.sleep(AUTH_SESSION_SETTLE_SECONDS)
    finally:
        try:
            await page.close()
        except Exception:
            pass


def index_path_for_dataset(out_dir: str, index_file: str) -> str:
    return os.path.join(out_dir, index_file)


def discover_index_candidates(out_dir: str, dataset_id: int, default_index_file: str) -> List[str]:
    prefix = f"index_data{dataset_id}"
    candidates: List[str] = []
    if not os.path.isdir(out_dir):
        return [os.path.join(out_dir, default_index_file)]

    for name in sorted(os.listdir(out_dir)):
        lower = name.lower()
        if not name.startswith(prefix):
            continue
        if not (lower.endswith(".json") or lower.endswith(".sqlite")):
            continue
        path = os.path.join(out_dir, name)
        if os.path.isfile(path):
            candidates.append(path)

    default_path = os.path.join(out_dir, default_index_file)
    if default_path not in candidates:
        candidates.insert(0, default_path)

    preferred = sorted(
        dict.fromkeys(candidates),
        key=lambda p: (
            0 if os.path.basename(p) == default_index_file else 1,
            0 if p.lower().endswith(".json") else 1,
            os.path.basename(p),
        ),
    )
    return preferred


def choose_index_path_interactive(dataset_id: int, out_dir: str, default_index_file: str) -> str:
    candidates = discover_index_candidates(out_dir, dataset_id, default_index_file)
    existing = [p for p in candidates if os.path.exists(p)]

    if not existing:
        return os.path.join(out_dir, default_index_file)
    if len(existing) == 1:
        return existing[0]

    print("\n" + ui_text(f"[DS {dataset_id}] Available index files:", "green", numbers_red=True))
    for idx_num, path in enumerate(existing, start=1):
        print(
            f"  {ansi(str(idx_num), 'red')} {ansi('=', 'red')} "
            f"{ui_text(os.path.basename(path), 'white', numbers_red=True)}"
        )

    default_choice = 1
    raw = input(
        "\n" + ui_text(
            f"Choose index file for dataset {dataset_id} [{default_choice}]: ",
            "red",
            numbers_red=True,
        )
    ).strip()

    try:
        choice = int(raw) if raw else default_choice
    except ValueError:
        choice = default_choice

    if not 1 <= choice <= len(existing):
        choice = default_choice
    return existing[choice - 1]


def init_index_structure(idx: Dict[str, Any], dataset_id: int) -> Dict[str, Any]:
    if not idx:
        idx = {
            "meta": {
                "dataset": dataset_id,
                "created_at": datetime.now().isoformat(timespec="seconds"),
                "last_scan_at": None,
                "last_scan_page": 0,
                "version": 3,
            },
            "files": {}
        }
        mark_index_force_full_save(idx)
        return ensure_index_runtime_tracking(idx)
    idx.setdefault("meta", {})
    idx.setdefault("files", {})
    idx["meta"].setdefault("dataset", dataset_id)
    idx["meta"].setdefault("version", 3)
    idx["meta"].setdefault("created_at", datetime.now().isoformat(timespec="seconds"))
    idx["meta"].setdefault("last_scan_at", None)
    idx["meta"].setdefault("last_scan_page", 0)
    return ensure_index_runtime_tracking(idx)


def reconcile_existing_downloads(idx: Dict[str, Any], out_dir: str) -> int:
    files = idx.get("files", {})
    if not isinstance(files, dict):
        return 0

    updated = 0
    try:
        existing_pdfs = {
            name for name in os.listdir(out_dir)
            if name.lower().endswith(".pdf")
        }
    except Exception:
        return 0

    for filename in existing_pdfs:
        entry = files.get(filename)
        if not isinstance(entry, dict):
            continue
        if entry.get("downloaded"):
            continue

        out_path = os.path.join(out_dir, filename)
        entry["downloaded"] = True
        entry["downloaded_at"] = entry.get("downloaded_at") or datetime.now().isoformat(timespec="seconds")
        try:
            entry["bytes"] = os.path.getsize(out_path)
        except Exception:
            pass
        mark_index_entry_dirty(idx, filename)
        updated += 1

    return updated


def hydrate_entry_from_existing_file(entry: Dict[str, Any], out_path: str) -> bool:
    if entry.get("downloaded") or not os.path.exists(out_path):
        return False

    entry["downloaded"] = True
    entry["downloaded_at"] = entry.get("downloaded_at") or datetime.now().isoformat(timespec="seconds")
    try:
        entry["bytes"] = os.path.getsize(out_path)
    except Exception:
        pass
    return True


def upsert_index_entry(
    idx: Dict[str, Any],
    filename: str,
    url: str,
    page_num: int,
    out_dir: Optional[str] = None,
) -> bool:
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
        if out_dir:
            out_path = os.path.join(out_dir, filename)
            hydrate_entry_from_existing_file(files[filename], out_path)
        mark_index_entry_dirty(idx, filename)
        return True

    files[filename]["url"] = url
    if out_dir:
        out_path = os.path.join(out_dir, filename)
        hydrate_entry_from_existing_file(files[filename], out_path)
    mark_index_entry_dirty(idx, filename)
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


def file_is_valid_pdf(path: str) -> bool:
    if not PYMUPDF_AVAILABLE:
        return True
    try:
        with fitz.open(path) as doc:
            _ = doc.page_count
        return True
    except Exception:
        return False


def should_deep_validate_pdf(entry: Dict[str, Any]) -> bool:
    return PYMUPDF_AVAILABLE and (
        int(entry.get("attempts", 0) or 0) >= 2 or
        int(entry.get("poison_hits", 0) or 0) >= 1 or
        int(entry.get("poison_refreshes", 0) or 0) >= 1
    )


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
    *,
    deep_validate: bool = False,
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

                if deep_validate and not file_is_valid_pdf(part_path):
                    try:
                        if os.path.exists(part_path):
                            os.remove(part_path)
                    except Exception:
                        pass
                    return False, "SESSION_POISON_BAD"

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


async def download_one(
    context,
    pdf_url: str,
    referer: str,
    out_path: str,
    *,
    deep_validate: bool = False,
) -> Tuple[bool, str]:
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
                return await stream_download_via_aiohttp(
                    context,
                    pdf_url,
                    referer,
                    out_path,
                    part_path,
                    deep_validate=deep_validate,
                )
        except Exception:
            pass

    try:
        body = await resp.body()
    except Exception as e:
        msg = str(e)
        if "Cannot create a string longer than" in msg:
            return await stream_download_via_aiohttp(
                context,
                pdf_url,
                referer,
                out_path,
                part_path,
                deep_validate=deep_validate,
            )
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

    if deep_validate and not file_is_valid_pdf(part_path):
        try:
            if os.path.exists(part_path):
                os.remove(part_path)
        except Exception:
            pass
        return False, "SESSION_POISON_BAD"

    os.replace(part_path, out_path)
    return True, ""


async def scan_dataset_pages(
    playwright,
    auth_browser,
    context,
    dataset_id: int,
    base_url: str,
    out_dir: str,
    state_file: str,
    idx_path: str,
    idx: Dict[str, Any],
    scan_options: Optional[Dict[str, Any]] = None,
    *,
    download_after_page: bool = False,
    click_age_gate: bool = False,
) -> Tuple[Dict[str, Any], Any, Any]:
    scan_options = scan_options or {"kind": "continue"}
    scan_kind = scan_options.get("kind", "continue")
    prior_last_scan_page = max(0, int(idx["meta"].get("last_scan_page", 0)) or 0)
    end_page: Optional[int] = None
    suppress_no_new_until_page: Optional[int] = None

    if scan_kind == "page1":
        start_page = 1
        if prior_last_scan_page >= start_page:
            suppress_no_new_until_page = prior_last_scan_page
    elif scan_kind == "range":
        start_page = max(1, int(scan_options.get("start_page", 1)))
        raw_end_page = scan_options.get("end_page")
        if raw_end_page is None:
            end_page = None
        else:
            end_page = max(start_page, int(raw_end_page))
        if prior_last_scan_page >= start_page:
            suppress_no_new_until_page = prior_last_scan_page
    else:
        resume_page = load_resume_page(state_file)
        if resume_page is not None and resume_page < prior_last_scan_page:
            log(
                f"[DS {dataset_id}] Resume pointer {resume_page} is behind index high-water "
                f"{prior_last_scan_page}; using index value"
            )
        start_page = max(1, resume_page or 0, prior_last_scan_page or 0)

    if end_page is not None:
        log(f"[DS {dataset_id}] Scan start at page {start_page} (range mode through page {end_page})")
    else:
        log(f"[DS {dataset_id}] Scan start at page {start_page} (mode={scan_kind})")
    if suppress_no_new_until_page is not None:
        log(
            f"[DS {dataset_id}] No-new-pages stop disabled until page "
            f"{suppress_no_new_until_page} during {scan_kind} scan"
        )

    pages_no_new = 0
    page_num = start_page
    pages_since_checkpoint = 0
    worker_browser = None
    worker_context = None
    page = None

    worker_browser, worker_context, page = await create_hidden_scan_worker(playwright, context)

    try:
        while True:
            if page_num > MAX_PAGES_HARD_CAP:
                log(f"[DS {dataset_id}] HARD CAP reached at page {page_num}. Stopping to avoid infinite loop.")
                break

            save_resume_page(state_file, page_num)
            page_url = base_url.format(page_num)
            SESSION["pages_scanned"] += 1
            _ds_stats(dataset_id)["pages_scanned"] += 1

            log(f"[DS {dataset_id}] Scanning page {page_num}")

            auth_refresh_attempts = 0
            page_fetch_failure_streak = 0

            while True:
                try:
                    await page.goto(page_url, wait_until="domcontentloaded")
                    await ensure_robot_verified(page, dataset_id=dataset_id)
                    await ensure_age_verified(
                        page,
                        dataset_id=dataset_id,
                        click_if_found=click_age_gate,
                        log_detection=False,
                    )
                    try:
                        await page.wait_for_load_state("networkidle", timeout=20000)
                    except PWTimeoutError:
                        pass
                    page_fetch_failure_streak = 0
                    break
                except Exception as e:
                    page_fetch_failure_streak += 1
                    SESSION["scan_page_fetch_failures"] += 1
                    _ds_stats(dataset_id)["scan_page_fetch_failures"] += 1
                    log(f"[DS {dataset_id}] [scan] PAGE FETCH FAILED on page {page_num}: {repr(e)}")

                    if auth_refresh_attempts < MAX_SCAN_AUTH_REFRESH_RETRIES:
                        SESSION["scan_auth_refreshes"] += 1
                        _ds_stats(dataset_id)["scan_auth_refreshes"] += 1
                        log(
                            f"[DS {dataset_id}] [scan] Refreshing auth/window state for page {page_num} "
                            f"(attempt {auth_refresh_attempts + 1}/{MAX_SCAN_AUTH_REFRESH_RETRIES})"
                        )
                        try:
                            auth_browser = await ensure_auth_browser(playwright, auth_browser)
                            context, _auth_page = await create_fresh_context(
                                auth_browser,
                                base_url.format(1),
                                dataset_id=dataset_id,
                                click_age_gate=click_age_gate,
                                log_age_gate_detection=False,
                            )
                            await close_hidden_scan_worker(page, worker_context, worker_browser)
                            worker_browser, worker_context, page = await create_hidden_scan_worker(playwright, context)
                            auth_refresh_attempts += 1
                            await asyncio.sleep(SCAN_AUTH_REFRESH_COOLDOWN)
                            continue
                        except Exception as refresh_err:
                            log(f"[DS {dataset_id}] [scan] Auth refresh failed on page {page_num}: {repr(refresh_err)}")
                            auth_refresh_attempts += 1
                            await asyncio.sleep(SCAN_AUTH_REFRESH_COOLDOWN)

                    if page_fetch_failure_streak >= MAX_SCAN_PAGE_HARD_FAILURES:
                        SESSION["scan_stops_on_error"] += 1
                        _ds_stats(dataset_id)["scan_stops_on_error"] += 1
                        log(
                            f"[DS {dataset_id}] [scan] Page {page_num} failed "
                            f"{page_fetch_failure_streak} times. Preserving current index state."
                        )
                        return idx, context, auth_browser

                    delay = max(
                        SCAN_PAGE_HARD_FAILURE_COOLDOWN,
                        backoff_sleep_seconds(page_fetch_failure_streak + 1),
                    )
                    log(
                        f"[DS {dataset_id}] [scan] Cooling down {delay:.2f}s and retrying page {page_num} "
                        f"(failure {page_fetch_failure_streak}/{MAX_SCAN_PAGE_HARD_FAILURES})"
                    )
                    await asyncio.sleep(delay)

            hrefs = await page.eval_on_selector_all(
                "a[href]",
                "els => els.map(e => e.getAttribute('href'))"
            )

            pdfs: List[Tuple[str, str]] = []
            seen = set()
            for href in hrefs:
                if not href:
                    continue
                full_url = urljoin(BASE_SITE, href)
                if is_valid_epstein_pdf_url(full_url):
                    filename = os.path.basename(urlparse(full_url).path)
                    if filename and filename not in seen:
                        seen.add(filename)
                        pdfs.append((filename, full_url))

            log(f"[DS {dataset_id}] Found {len(pdfs)} PDFs on page {page_num}")

            new_this_page = 0
            for filename, full_url in pdfs:
                if extract_file_num(filename) is None:
                    continue
                if upsert_index_entry(idx, filename, full_url, page_num, out_dir=out_dir):
                    new_this_page += 1
            if new_this_page:
                SESSION["new_pdfs_found"] += new_this_page
                _ds_stats(dataset_id)["new_pdfs_found"] += new_this_page

            streak_suppressed = (
                suppress_no_new_until_page is not None and
                page_num <= suppress_no_new_until_page
            )

            if new_this_page == 0:
                if streak_suppressed:
                    log(
                        f"[DS {dataset_id}] No NEW PDFs on page {page_num} "
                        f"(streak suppressed until page {suppress_no_new_until_page})"
                    )
                else:
                    pages_no_new += 1
                    log(f"[DS {dataset_id}] No NEW PDFs on page {page_num} (streak={pages_no_new}/{MAX_PAGES_WITH_NO_NEW_PDFS})")
            else:
                if not streak_suppressed:
                    pages_no_new = 0
                log(f"[DS {dataset_id}] NEW PDFs discovered on page {page_num}: {new_this_page}")

            idx["meta"]["last_scan_at"] = datetime.now().isoformat(timespec="seconds")
            idx["meta"]["last_scan_page"] = max(prior_last_scan_page, page_num)
            mark_index_meta_dirty(idx)
            pages_since_checkpoint += 1

            should_checkpoint = (
                new_this_page > 0 or
                idx["meta"]["last_scan_page"] > prior_last_scan_page or
                pages_since_checkpoint >= SCAN_CHECKPOINT_EVERY_PAGES
            )
            if should_checkpoint:
                save_index_data(idx_path, idx)
                pages_since_checkpoint = 0

            if download_after_page:
                page_filenames = {
                    filename for filename, _ in pdfs
                    if extract_file_num(filename) is not None
                }
                if page_filenames:
                    completed_now, context = await download_missing_from_index(
                        auth_browser,
                        context,
                        dataset_id,
                        out_dir,
                        idx_path,
                        idx,
                        base_url,
                        filename_filter=page_filenames,
                    )
                    if completed_now:
                        log(
                            f"[DS {dataset_id}] Page {page_num} immediate download pass complete ΓÇö "
                            f"{completed_now} new PDFs"
                        )

            if suppress_no_new_until_page is not None and page_num >= suppress_no_new_until_page:
                log(
                    f"[DS {dataset_id}] Reached prior last scanned page {suppress_no_new_until_page}; "
                    f"re-enabling no-new-pages stop logic"
                )
                suppress_no_new_until_page = None

            if end_page is not None and page_num >= end_page:
                log(f"[DS {dataset_id}] Reached end of requested scan range at page {page_num}.")
                break

            if pages_no_new >= MAX_PAGES_WITH_NO_NEW_PDFS:
                log(f"[DS {dataset_id}] Stopping scan: no new PDFs for {MAX_PAGES_WITH_NO_NEW_PDFS} consecutive pages.")
                break

            page_num += 1
            await asyncio.sleep(SLEEP_BETWEEN_PAGES)

        if pages_since_checkpoint > 0:
            save_index_data(idx_path, idx)
        return idx, context, auth_browser
    finally:
        await close_hidden_scan_worker(page, worker_context, worker_browser)


async def download_missing_from_index(
    browser,
    context,
    dataset_id: int,
    out_dir: str,
    idx_path: str,
    idx: Dict[str, Any],
    base_url: str,
    filename_filter: Optional[set[str]] = None,
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
        if filename_filter is not None and filename not in filename_filter:
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
            mark_index_entry_dirty(idx, filename)
            SESSION["marked_downloaded_existing"] += 1
            _ds_stats(dataset_id)["marked_downloaded_existing"] += 1
            save_index_data(idx_path, idx)
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
            mark_index_entry_dirty(idx, filename)
            save_index_data(idx_path, idx)
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
                mark_index_entry_dirty(idx, filename)
                save_index_data(idx_path, idx)
                break  # move to next file

            log(f"[DS {dataset_id}] DOWNLOAD {filename}")
            deep_validate = should_deep_validate_pdf(entry)
            ok, err = await download_one(
                context,
                pdf_url,
                referer,
                out_path,
                deep_validate=deep_validate,
            )

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
                mark_index_entry_dirty(idx, filename)

                completed += 1
                SESSION["downloaded_ok"] += 1
                _ds_stats(dataset_id)["downloaded_ok"] += 1

                log(f"[DS {dataset_id}] DONE ({completed}) {filename}")
                save_index_data(idx_path, idx)
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
                    mark_index_entry_dirty(idx, filename)
                    save_index_data(idx_path, idx)
                    await asyncio.sleep(SLEEP_BETWEEN_DOWNLOADS)
                    break  # move to next file

                # HTML gate / poison
                entry["last_error"] = f"{err} (non-PDF response)"
                log(f"[DS {dataset_id}] SESSION POISON ({err}) while downloading {filename} (hit {poison_hits}/{POISON_HITS_BEFORE_SKIP})")
                mark_index_entry_dirty(idx, filename)
                save_index_data(idx_path, idx)

                if poison_hits >= POISON_HITS_BEFORE_SKIP:
                    entry["skipped"] = True
                    entry["skip_reason"] = f"REPEATED_SESSION_POISON ({err})"
                    entry["last_error"] = entry["skip_reason"]

                    SESSION["skipped_poison_hit_limit"] += 1
                    _ds_stats(dataset_id)["skipped_poison_hit_limit"] += 1

                    log(f"[DS {dataset_id}] POISON HIT LIMIT -> SKIPPING {filename}")
                    log_bad_file(dataset_id, filename, pdf_url, referer, entry["skip_reason"], extra=f"poison_hits={poison_hits}")
                    mark_index_entry_dirty(idx, filename)
                    save_index_data(idx_path, idx)
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
                    mark_index_entry_dirty(idx, filename)
                    save_index_data(idx_path, idx)
                    await asyncio.sleep(SLEEP_BETWEEN_DOWNLOADS)
                    break  # move to next file

                # Refresh session and then retry SAME file (inner-loop continue)
                loud_session_poison_alert(dataset_id, filename)
                log(f"[DS {dataset_id}] Auto-refreshing session context (hands-free)... (refresh {refreshes+1}/{POISON_REFRESHES_BEFORE_SKIP})")
                entry["poison_refreshes"] = refreshes + 1
                mark_index_entry_dirty(idx, filename)
                save_index_data(idx_path, idx)

                try:
                    await context.close()
                except Exception:
                    pass

                context, _auth_page = await create_fresh_context(
                    browser,
                    base_url.format(1),
                    dataset_id=dataset_id,
                    click_age_gate=True,
                    log_age_gate_detection=True,
                )
                log(f"[DS {dataset_id}] Session refreshed. Retrying {filename}...")
                await asyncio.sleep(0.2)
                continue  # <-- ACTUALLY retries the same filename now

            # --- Normal error path ---
            entry["attempts"] = int(entry.get("attempts", 0)) + 1
            entry["last_error"] = err

            log(f"[DS {dataset_id}] ERROR {err} for {filename}")
            mark_index_entry_dirty(idx, filename)
            save_index_data(idx_path, idx)

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


async def process_dataset(
    playwright,
    browser,
    dataset_id: int,
    cfg: Dict[str, Any],
    mode: str,
    scan_options: Optional[Dict[str, Any]] = None,
) -> None:
    base_url = cfg["base_url"]
    out_dir = cfg["out_dir"]
    state_file = cfg["state_file"]
    index_file = cfg["index_file"]

    os.makedirs(out_dir, exist_ok=True)

    idx_path = choose_index_path_interactive(dataset_id, out_dir, index_file)
    idx = load_index_data(idx_path)
    idx = init_index_structure(idx, dataset_id)
    reconciled_existing = reconcile_existing_downloads(idx, out_dir)

    log(f"=== DATASET {dataset_id} START (mode={mode}) ===")
    log(f"Output dir: {out_dir}")
    log(f"Index file: {idx_path}")
    if reconciled_existing:
        log(f"[DS {dataset_id}] Reconciled {reconciled_existing} existing local PDFs into index state")
        save_index_data(idx_path, idx)

    active_browser = browser
    context = None
    auth_page = None

    try:
        context, auth_page = await create_fresh_context(
            active_browser,
            base_url.format(1),
            dataset_id=dataset_id,
            click_age_gate=True,
            log_age_gate_detection=(mode != "scan"),
        )

        if mode in {"scan", "sync"}:
            idx, context, active_browser = await scan_dataset_pages(
                playwright,
                active_browser,
                context,
                dataset_id,
                base_url,
                out_dir,
                state_file,
                idx_path,
                idx,
                scan_options=scan_options if mode == "scan" else None,
                download_after_page=(mode == "sync"),
                click_age_gate=True,
            )

        if mode == "download":
            completed, context = await download_missing_from_index(active_browser, context, dataset_id, out_dir, idx_path, idx, base_url)
            log(f"[DS {dataset_id}] Download pass complete ΓÇö {completed} new PDFs")

        save_index_data(idx_path, idx)

    except Exception as e:
        log(f"[DS {dataset_id}] FATAL ERROR: {repr(e)}")
        try:
            save_index_data(idx_path, idx)
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
        if active_browser is not None and active_browser is not browser:
            try:
                await active_browser.close()
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
    install_loop_exception_handler()

    banner = """

     
#     Prizm presents
#   ▄████████    ▄███████▄    ▄████████  ▄█     ▄███████▄
#  ███    ███   ███    ███   ███    ███ ███    ███    ███
#  ███    █▀    ███    ███   ███    ███ ███▌   ███    ███
# ▄███▄▄▄       ███    ███  ▄███▄▄▄▄██▀ ███▌   ███    ███
#▀▀███▀▀▀      ▀█████████▀  ▀███▀▀▀▀▀   ███▌ ▀█████████▀
#  ███    █▄    ███        ▀███████████ ███    ███
#  ███    ███   ███ STEIN  - ███    ███ ███    ███ PER
#  ██████████  ▄████▀        ███    ███ █▀    ▄████▀
#   [ AUTOMATIC ]                       ███    ███ version 3.5
#                        A Prizmatik Underground Production
# ==========================================================
"""
    print(ansi(banner, "green"))

    args = parse_args()

    chosen = parse_datasets_string(args.datasets)
    if not chosen:
        chosen = ask_datasets_interactive()

    mode = args.mode if args.mode in {"scan", "download", "sync"} else ask_mode_interactive()
    scan_options = None
    if mode == "scan":
        scan_options = ask_scan_start_options_interactive()

    log(f"Selected datasets: {chosen}")
    log(f"Mode: {mode}")
    if scan_options is not None:
        log(f"Scan start options: {scan_options}")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=args.headless, slow_mo=25)

        for dataset_id in chosen:
            await process_dataset(
                p,
                browser,
                dataset_id,
                DATASETS[dataset_id],
                mode,
                scan_options=scan_options,
            )

        await browser.close()

    log("ALL DATASETS COMPLETE")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log("KeyboardInterrupt received ΓÇö shutting down.")
    finally:
        print_session_summary()
