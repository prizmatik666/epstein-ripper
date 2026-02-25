#!/usr/bin/env python3
"""
Active Watcher ΓÇö HTML/age-verify corruption detector + quarantine mover
Hardened edition (stupid-proof, mixed-folder safe):

- Watches ONLY PDFs in the selected directory (non-recursive)
- Detects HTML/age-verify corruption by scanning header bytes
- Moves corrupted PDFs into <watch_dir>/quarantine/
- Non-blocking pause: waits for Enter to acknowledge but continues scanning silently
- Writes corruption_events.log (append-only)
- Uses watcher_state.json for resume, and self-heals if state is old/invalid
- Prevents "silent freeze" by adding a max wait timeout to stable-size checks

This version is safe even if the watched folder contains .py/.txt/log/etc.
Those files are ignored and never enter watcher_state.json.
"""
# ============================================================
# Epstein DOJ Dataset Tools
#
# Author: Prizm (Prizmatik Underground)
# Repository:
# https://github.com/prizmatik666/epstein-ripper
#
# Support Development:
# PayPal: https://www.paypal.com/ncp/payment/VVDAXZGKPQZKW
# Email: prizmatikug@gmail.com
#
# ============================================================
import os
import sys
import time
import json
import hashlib
import select
from pathlib import Path
from datetime import datetime

# ===================== CONFIG =====================
WATCH_EXTENSIONS = {".pdf"}         # <-- HARD FILTER: only PDFs are watched/tracked

POLL_INTERVAL_SEC = 0.5
STABLE_CHECKS = 2
STABLE_INTERVAL_SEC = 0.25
STABLE_MAX_WAIT_SEC = 10.0         # <-- prevents infinite stalls

HEADER_READ_BYTES = 8192
STATE_FILENAME = "watcher_state.json"
QUARANTINE_DIRNAME = "quarantine"
EVENTS_LOG_FILENAME = "corruption_events.log"

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

def is_watched_file(name: str) -> bool:
    return Path(name).suffix.lower() in WATCH_EXTENSIONS

def load_state(state_path: Path) -> dict:
    if state_path.exists():
        try:
            return json.loads(state_path.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}

def save_state(state_path: Path, state: dict):
    tmp = state_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(state_path)

def backup_bad_state(state_path: Path):
    try:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        bak = state_path.with_name(f"{state_path.name}.bak_{ts}")
        state_path.replace(bak)
    except Exception:
        pass

def validate_or_reset_state(state_path: Path, watch_dir: Path) -> dict:
    """
    Expected schema:
      {"watch_dir": "...", "files": { "name.pdf": {"size":..,"mtime_ns":..,"hh":..}, ... }}
    Any mismatch => backup + reset (no user intervention).
    """
    state = load_state(state_path)
    ok = (
        isinstance(state, dict)
        and state.get("watch_dir") == str(watch_dir)
        and isinstance(state.get("files"), dict)
    )
    if not ok:
        if state_path.exists():
            backup_bad_state(state_path)
        state = {"watch_dir": str(watch_dir), "files": {}}
        save_state(state_path, state)
    return state

def append_event(log_path: Path, line: str):
    try:
        with log_path.open("a", encoding="utf-8") as f:
            f.write(line.rstrip("\n") + "\n")
    except Exception:
        pass

def list_root_pdfs(watch_dir: Path):
    """Non-recursive list of ONLY PDFs in watch_dir root, excluding quarantine directory."""
    out = []
    for entry in os.scandir(watch_dir):
        if entry.is_dir() and entry.name == QUARANTINE_DIRNAME:
            continue
        if entry.is_file() and is_watched_file(entry.name):
            out.append(entry.name)
    return out

def wait_until_stable(file_path: Path) -> bool:
    """Wait until file size stops changing, but never block forever."""
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
    try:
        with file_path.open("rb") as f:
            head = f.read(HEADER_READ_BYTES)
    except Exception:
        return True

    if not head:
        return True

    # Valid PDF signature
    if head.startswith(b"%PDF-"):
        return False

    head_lc = head.lower()
    for m in HTML_MARKERS:
        if m in head_lc:
            return True

    if head[:1] == b"<":
        if b"</" in head_lc or b"document" in head_lc or b"script" in head_lc:
            return True

    return False

def header_hash(file_path: Path) -> str:
    try:
        with file_path.open("rb") as f:
            head = f.read(HEADER_READ_BYTES)
    except Exception:
        return "READ_ERROR"
    return hashlib.sha1(head).hexdigest()

def file_fingerprint(file_path: Path) -> dict:
    try:
        st = file_path.stat()
        return {
            "size": int(st.st_size),
            "mtime_ns": int(getattr(st, "st_mtime_ns", int(st.st_mtime * 1e9))),
            "hh": header_hash(file_path),
        }
    except FileNotFoundError:
        return {"missing": True}

def big_corruption_warning(file_name: str):
    msg = "ERROR! CORRUPTED FILE DOWNLOADED!"
    print("\n" + "=" * 70)
    for _ in range(10):
        print(msg, "->", file_name)
    print("=" * 70 + "\n")
    term_bell(times=10)

def move_to_quarantine(src: Path, quarantine_dir: Path) -> Path | None:
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
    try:
        r, _, _ = select.select([sys.stdin], [], [], 0)
        if r:
            _ = sys.stdin.readline()
            return True
    except Exception:
        pass
    return False

def main():
    print("=== Active Watcher (HTML corruption detector + quarantine) ===")
    watch_dir_input = input("Directory to watch: ").strip().strip('"').strip("'")
    if not watch_dir_input:
        print("No directory entered. Exiting.")
        return

    watch_dir = Path(watch_dir_input).expanduser().resolve()
    if not watch_dir.exists() or not watch_dir.is_dir():
        print(f"Invalid directory: {watch_dir}")
        return

    quarantine_dir = (watch_dir / QUARANTINE_DIRNAME).resolve()

    run_dir = Path.cwd()
    state_path = run_dir / STATE_FILENAME
    events_log_path = run_dir / EVENTS_LOG_FILENAME

    state = validate_or_reset_state(state_path, watch_dir)
    known: dict = state.get("files", {})

    # HARDEN: purge any non-PDF keys if legacy state ever had them
    nonpdf_keys = [k for k in known.keys() if not is_watched_file(k)]
    if nonpdf_keys:
        for k in nonpdf_keys:
            known.pop(k, None)
        state["files"] = known
        save_state(state_path, state)

    paused = False
    last_pause_reminder = 0.0

    # Startup visibility so you KNOW it sees PDFs immediately
    initial_pdfs = list_root_pdfs(watch_dir)
    print(f"[{now_ts()}] Watching: {watch_dir}")
    print(f"[{now_ts()}] Watching extensions: {', '.join(sorted(WATCH_EXTENSIONS))}")
    print(f"[{now_ts()}] PDFs currently present: {len(initial_pdfs)}")
    print(f"[{now_ts()}] Quarantine: {quarantine_dir}")
    print(f"[{now_ts()}] State: {state_path}")
    print(f"[{now_ts()}] Events Log: {events_log_path}")
    print("Press Ctrl+C to stop.\n")

    append_event(
        events_log_path,
        f"{now_ts()} | START | watch_dir={watch_dir} | quarantine={quarantine_dir} | state={state_path}"
    )

    try:
        while True:
            if paused and enter_pressed_nonblocking():
                paused = False
                print(f"\n[{now_ts()}] Acknowledged. Resuming normal output.\n")
                append_event(events_log_path, f"{now_ts()} | ACK | user_acknowledged_pause")

            current_names = set(list_root_pdfs(watch_dir))  # <-- ONLY PDFs

            # Fast prune: remove state entries for PDFs that no longer exist
            removed_names = set(known.keys()) - current_names
            if removed_names:
                for n in removed_names:
                    known.pop(n, None)
                state["files"] = known
                save_state(state_path, state)

            # Sort PDFs by mtime then name
            items = []
            for name in current_names:
                p = watch_dir / name
                try:
                    st = p.stat()
                    items.append((st.st_mtime, name))
                except FileNotFoundError:
                    continue
            items.sort(key=lambda x: (x[0], x[1]))

            for _, name in items:
                p = watch_dir / name

                # Wait for stable write, but skip if it won't settle quickly
                if not wait_until_stable(p):
                    continue

                fp = file_fingerprint(p)
                prev = known.get(name)

                if prev != fp:
                    bad = header_is_html(p)
                    if bad:
                        big_corruption_warning(name)

                        dest = move_to_quarantine(p, quarantine_dir)
                        if dest:
                            rel = f"{QUARANTINE_DIRNAME}/{dest.name}"
                            print(f"[{now_ts()}] Moved to quarantine: {dest.name}")
                            append_event(
                                events_log_path,
                                f"{now_ts()} | CORRUPTED | {name} | moved_to={rel} | reason=html_header_detected"
                            )
                        else:
                            print(f"[{now_ts()}] WARNING: failed to move {name} to quarantine!")
                            append_event(
                                events_log_path,
                                f"{now_ts()} | CORRUPTED | {name} | moved_to=FAILED | reason=html_header_detected"
                            )

                        if not paused:
                            paused = True
                            last_pause_reminder = time.time()
                            print(f"\n[{now_ts()}] PAUSED ΓÇö press Enter to acknowledge. (Scanning continues silently.)\n")
                            append_event(events_log_path, f"{now_ts()} | PAUSE | corruption_detected_waiting_for_ack")

                        known.pop(name, None)

                    else:
                        if not paused:
                            print(f"{name} checked - PASS")
                        known[name] = fp

                    state["files"] = known
                    save_state(state_path, state)

            if paused:
                t = time.time()
                if (t - last_pause_reminder) >= PAUSE_REMINDER_EVERY_SEC:
                    last_pause_reminder = t
                    term_bell(times=2)
                    print(f"[{now_ts()}] PAUSED ΓÇö press Enter to acknowledge. (Scanning continues silently.)")

            time.sleep(POLL_INTERVAL_SEC)

    except KeyboardInterrupt:
        print("\nStopped by user (Ctrl+C). Saving state...")
        state["files"] = known
        save_state(state_path, state)
        append_event(events_log_path, f"{now_ts()} | STOP | user_interrupt")
        print(f"State saved to: {state_path}")
        print(f"Events log: {events_log_path}")

if __name__ == "__main__":
    main()
