#!/usr/bin/env python3
"""
Active Watcher (vNext) — NEW-ONLY PDF corruption detector + quarantine mover

Design goal (by intent):
- Takes a baseline snapshot of what exists at startup
- ONLY scans PDFs that appear AFTER the program starts
- Never scans older/backlog files that existed before launch

Features kept from prior builds:
- Waits for file to finish writing (size stable)
- Scans header bytes for HTML/age-verify/server-error pages
- Loud alert: prints error banner 10x + terminal bell
- Moves corrupted file into <watch_dir>/quarantine/
- Enters PAUSED mode waiting for Enter to acknowledge
  BUT continues scanning + quarantining silently in the background
- Writes append-only corruption_events.log for audit/review

Notes:
- This watcher is intentionally session-based (no resume state file),
  because the whole point is: scan only "incoming after start".
"""

import os
import sys
import time
import hashlib
import select
from pathlib import Path
from datetime import datetime

# ===================== CONFIG =====================
POLL_INTERVAL_SEC = 0.5

WATCH_EXTENSIONS = {".pdf"}          # only scan these
QUARANTINE_DIRNAME = "quarantine"
EVENTS_LOG_FILENAME = "corruption_events.log"

HEADER_READ_BYTES = 8192

# Stable-write detection
STABLE_CHECKS = 2
STABLE_INTERVAL_SEC = 0.25
STABLE_MAX_WAIT_SEC = 12.0           # prevents per-file infinite stall

# While paused, how often to reprint the reminder (seconds)
PAUSE_REMINDER_EVERY_SEC = 15
# ==================================================

HTML_MARKERS = [
    b"<html",
    b"<!doctype html",
    b"<head",
    b"<title",
]

def now_ts() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def term_bell(times: int = 6):
    sys.stdout.write("\a" * times)
    sys.stdout.flush()

def append_event(log_path: Path, line: str):
    try:
        with log_path.open("a", encoding="utf-8") as f:
            f.write(line.rstrip("\n") + "\n")
    except Exception:
        pass  # never crash due to logging

def is_watched_file(name: str) -> bool:
    return Path(name).suffix.lower() in WATCH_EXTENSIONS

def list_root_watched_files(watch_dir: Path):
    """Non-recursive list of watched files in root, excluding quarantine dir."""
    out = []
    for entry in os.scandir(watch_dir):
        if entry.is_dir() and entry.name == QUARANTINE_DIRNAME:
            continue
        if entry.is_file() and is_watched_file(entry.name):
            out.append(entry.name)
    return out

def wait_until_stable(file_path: Path) -> bool:
    """
    Wait until file size stops changing for STABLE_CHECKS intervals,
    but never block longer than STABLE_MAX_WAIT_SEC.
    """
    last_size = None
    stable = 0
    start = time.time()

    while stable < STABLE_CHECKS:
        if (time.time() - start) > STABLE_MAX_WAIT_SEC:
            return False

        if not file_path.exists():
            return False

        try:
            size = file_path.stat().st_size
        except FileNotFoundError:
            return False

        if last_size is not None and size == last_size:
            stable += 1
        else:
            stable = 0

        last_size = size
        time.sleep(STABLE_INTERVAL_SEC)

    return True

def header_is_html(file_path: Path) -> bool:
    """Detect HTML/age-verify pages by checking header bytes."""
    try:
        with file_path.open("rb") as f:
            head = f.read(HEADER_READ_BYTES)
    except Exception:
        return True

    if not head:
        return True

    # Strong pass: valid PDF signature
    if head.startswith(b"%PDF-"):
        return False

    head_lc = head.lower()

    for m in HTML_MARKERS:
        if m in head_lc:
            return True

    # Additional heuristic: leading '<' + common HTML/JS hints
    if head[:1] == b"<":
        if b"</" in head_lc or b"document" in head_lc or b"script" in head_lc:
            return True

    return False

def big_corruption_warning(file_name: str):
    msg = "ERROR! CORRUPTED FILE DOWNLOADED!"
    print("\n" + "=" * 70)
    for _ in range(10):
        print(msg, "->", file_name)
    print("=" * 70 + "\n")
    term_bell(times=10)

def move_to_quarantine(src: Path, quarantine_dir: Path) -> Path | None:
    """Move src into quarantine_dir; auto-rename if collision."""
    quarantine_dir.mkdir(parents=True, exist_ok=True)

    base = src.stem
    ext = src.suffix
    dest = quarantine_dir / (base + ext)

    if dest.exists():
        i = 1
        while True:
            cand = quarantine_dir / f"{base}__q{i}{ext}"
            if not cand.exists():
                dest = cand
                break
            i += 1

    try:
        src.replace(dest)
        return dest
    except Exception:
        try:
            import shutil
            shutil.copy2(src, dest)
            src.unlink(missing_ok=True)
            return dest
        except Exception:
            return None

def enter_pressed_nonblocking() -> bool:
    """Non-blocking Enter detection (WSL/Linux)."""
    try:
        r, _, _ = select.select([sys.stdin], [], [], 0)
        if r:
            _ = sys.stdin.readline()
            return True
    except Exception:
        pass
    return False

def main():
    print("=== Active Watcher vNext (NEW-ONLY PDF corruption detector + quarantine) ===")
    watch_dir_input = input("Directory to watch: ").strip().strip('"').strip("'")
    if not watch_dir_input:
        print("No directory entered. Exiting.")
        return

    watch_dir = Path(watch_dir_input).expanduser().resolve()
    if not watch_dir.exists() or not watch_dir.is_dir():
        print(f"Invalid directory: {watch_dir}")
        return

    quarantine_dir = (watch_dir / QUARANTINE_DIRNAME).resolve()
    events_log_path = Path.cwd() / EVENTS_LOG_FILENAME

    # --- BASELINE SNAPSHOT: ignore everything that exists at startup ---
    baseline = set(list_root_watched_files(watch_dir))
    seen_after_start = set()  # names first seen after startup (session-only)

    paused = False
    last_pause_reminder = 0.0

    print(f"[{now_ts()}] Watching: {watch_dir}")
    print(f"[{now_ts()}] Mode: NEW-ONLY (baseline snapshot taken at startup)")
    print(f"[{now_ts()}] Baseline PDFs ignored: {len(baseline)}")
    print(f"[{now_ts()}] Quarantine: {quarantine_dir}")
    print(f"[{now_ts()}] Events Log: {events_log_path}")
    print("Press Ctrl+C to stop.\n")

    append_event(
        events_log_path,
        f"{now_ts()} | START | mode=new_only | watch_dir={watch_dir} | quarantine={quarantine_dir}"
    )
    append_event(events_log_path, f"{now_ts()} | BASELINE | ignored_existing_pdfs={len(baseline)}")

    try:
        while True:
            # If paused, allow Enter to acknowledge without blocking scanning
            if paused and enter_pressed_nonblocking():
                paused = False
                print(f"\n[{now_ts()}] Acknowledged. Resuming normal output.\n")
                append_event(events_log_path, f"{now_ts()} | ACK | user_acknowledged_pause")

            current = set(list_root_watched_files(watch_dir))

            # NEW-ONLY logic:
            # new arrivals = current - baseline - already_seen_after_start - quarantine dir excluded by lister
            new_arrivals = current - baseline - seen_after_start
            if new_arrivals:
                # Sort new arrivals by mtime (nice ordering)
                items = []
                for name in new_arrivals:
                    p = watch_dir / name
                    try:
                        st = p.stat()
                        items.append((st.st_mtime, name))
                    except FileNotFoundError:
                        continue
                items.sort(key=lambda x: (x[0], x[1]))

                for _, name in items:
                    p = watch_dir / name

                    # Mark as seen (so we don’t spin on it forever if it’s slow)
                    seen_after_start.add(name)

                    # Wait for download to finish writing (best effort)
                    if not wait_until_stable(p):
                        # If it didn't stabilize, we'll still consider it "seen" this session.
                        # It will get rescanned only if it disappears and reappears (new file event),
                        # which matches the "new-only" contract.
                        append_event(events_log_path, f"{now_ts()} | SKIP | {name} | reason=not_stable_in_time")
                        continue

                    # Scan header for HTML/corruption
                    bad = header_is_html(p)
                    if bad:
                        # Loud warning regardless of paused state
                        big_corruption_warning(name)

                        dest = move_to_quarantine(p, quarantine_dir)
                        if dest:
                            rel = f"{QUARANTINE_DIRNAME}/{dest.name}"
                            if not paused:
                                print(f"[{now_ts()}] Moved to quarantine: {dest.name}")
                            append_event(
                                events_log_path,
                                f"{now_ts()} | CORRUPTED | {name} | moved_to={rel} | reason=html_header_detected"
                            )
                        else:
                            if not paused:
                                print(f"[{now_ts()}] WARNING: failed to move {name} to quarantine!")
                            append_event(
                                events_log_path,
                                f"{now_ts()} | CORRUPTED | {name} | moved_to=FAILED | reason=html_header_detected"
                            )

                        # Enter paused mode, but do NOT block scanning
                        if not paused:
                            paused = True
                            last_pause_reminder = time.time()
                            print(f"\n[{now_ts()}] PAUSED — press Enter to acknowledge. (Scanning continues silently.)\n")
                            append_event(events_log_path, f"{now_ts()} | PAUSE | corruption_detected_waiting_for_ack")

                    else:
                        if not paused:
                            print(f"{name} checked - PASS")
                        append_event(events_log_path, f"{now_ts()} | PASS | {name}")

            # While paused, reprint reminder occasionally (so you notice)
            if paused:
                t = time.time()
                if (t - last_pause_reminder) >= PAUSE_REMINDER_EVERY_SEC:
                    last_pause_reminder = t
                    term_bell(times=2)
                    print(f"[{now_ts()}] PAUSED — press Enter to acknowledge. (Scanning continues silently.)")

            time.sleep(POLL_INTERVAL_SEC)

    except KeyboardInterrupt:
        print("\nStopped by user (Ctrl+C).")
        append_event(events_log_path, f"{now_ts()} | STOP | user_interrupt")

if __name__ == "__main__":
    main()
