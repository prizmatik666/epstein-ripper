#!/usr/bin/env python3
"""
Active Watcher — HTML/age-verify corruption detector + quarantine mover
STUPID-PROOF edition:
- Resumes safely after restarts via watcher_state.json
- Never skips re-downloaded same-name files (fingerprint-based)
- Can "pause" for user acknowledgment WITHOUT stopping scanning
- Writes corruption_events.log (append-only) with timestamped quarantine events

Behavior:
- When corruption is detected:
  - prints ERROR banner 10x + terminal bell
  - moves file into <watch_dir>/quarantine/
  - enters PAUSED mode: asks user to press Enter to acknowledge
  - BUT continues scanning/quarantining in the background (silently)
"""

import os
import sys
import time
import json
import hashlib
import select
from pathlib import Path
from datetime import datetime

# ===================== CONFIG =====================
POLL_INTERVAL_SEC = 0.5
STABLE_CHECKS = 2
STABLE_INTERVAL_SEC = 0.25
HEADER_READ_BYTES = 8192
STATE_FILENAME = "watcher_state.json"
QUARANTINE_DIRNAME = "quarantine"

EVENTS_LOG_FILENAME = "corruption_events.log"

# While paused, how often to re-print the pause reminder (seconds)
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

def append_event(log_path: Path, line: str):
    """
    Append a single line to corruption_events.log.
    Intentionally simple + robust: never crashes the watcher if logging fails.
    """
    try:
        with log_path.open("a", encoding="utf-8") as f:
            f.write(line.rstrip("\n") + "\n")
    except Exception:
        pass

def list_root_files(watch_dir: Path):
    """Non-recursive list of files in watch_dir root, excluding quarantine directory."""
    out = []
    for entry in os.scandir(watch_dir):
        if entry.is_dir() and entry.name == QUARANTINE_DIRNAME:
            continue
        if entry.is_file():
            out.append(entry.name)
    return out

def wait_until_stable(file_path: Path) -> bool:
    """Wait until file size stops changing."""
    last_size = None
    stable = 0
    while stable < STABLE_CHECKS:
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
    """Detect HTML/age-verify in header."""
    try:
        with file_path.open("rb") as f:
            head = f.read(HEADER_READ_BYTES)
    except Exception:
        return True

    if not head:
        return True

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
    """Hash first HEADER_READ_BYTES bytes (fast fingerprint component)."""
    try:
        with file_path.open("rb") as f:
            head = f.read(HEADER_READ_BYTES)
    except Exception:
        return "READ_ERROR"
    return hashlib.sha1(head).hexdigest()

def file_fingerprint(file_path: Path) -> dict:
    """
    Fingerprint used to decide if we must rescan.
    Includes:
      - size
      - mtime_ns
      - header hash
    """
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
    """Move src into quarantine_dir; auto-rename on collisions."""
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
    """
    Non-blocking check for Enter key on stdin (Linux/WSL).
    If user typed anything and hit Enter, we consume that line and return True.
    """
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

    state = load_state(state_path)

    # State is per-watch_dir and fingerprint-based
    # state = {"watch_dir": "...", "files": {"name.pdf": {"size":..,"mtime_ns":..,"hh":..}, ...}}
    if state.get("watch_dir") != str(watch_dir):
        state = {"watch_dir": str(watch_dir), "files": {}}
        save_state(state_path, state)

    known: dict = state.get("files", {})

    paused = False
    last_pause_reminder = 0.0

    print(f"[{now_ts()}] Watching: {watch_dir}")
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
            # If paused, allow Enter to acknowledge without blocking scanning
            if paused and enter_pressed_nonblocking():
                paused = False
                print(f"\n[{now_ts()}] Acknowledged. Resuming normal output.\n")
                append_event(events_log_path, f"{now_ts()} | ACK | user_acknowledged_pause")

            # Refresh file set
            current_names = set(list_root_files(watch_dir))

            # Prune state entries for files not in root anymore
            removed = [n for n in list(known.keys()) if n not in current_names]
            if removed:
                for n in removed:
                    known.pop(n, None)
                state["files"] = known
                save_state(state_path, state)

            # Sort by mtime then name
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

                # Wait for file to finish writing
                if not wait_until_stable(p):
                    continue

                fp = file_fingerprint(p)
                prev = known.get(name)

                # Only scan if new or changed
                if prev != fp:
                    bad = header_is_html(p)
                    if bad:
                        # Loud warning regardless of paused state
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

                        # Enter paused mode, but do NOT block
                        if not paused:
                            paused = True
                            last_pause_reminder = time.time()
                            print(f"\n[{now_ts()}] PAUSED — press Enter to acknowledge. (Scanning continues silently.)\n")
                            append_event(events_log_path, f"{now_ts()} | PAUSE | corruption_detected_waiting_for_ack")

                        # After moving, remove from known (no longer exists in root)
                        known.pop(name, None)

                    else:
                        # While paused, keep PASS output silent to reduce spam
                        if not paused:
                            print(f"{name} checked - PASS")
                        known[name] = fp

                    state["files"] = known
                    save_state(state_path, state)

            # While paused, reprint a reminder occasionally (so you notice the terminal)
            if paused:
                t = time.time()
                if (t - last_pause_reminder) >= PAUSE_REMINDER_EVERY_SEC:
                    last_pause_reminder = t
                    term_bell(times=2)
                    print(f"[{now_ts()}] PAUSED — press Enter to acknowledge. (Scanning continues silently.)")

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