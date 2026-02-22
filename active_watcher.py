#!/usr/bin/env python3
"""
Directory Watcher ΓÇö HTML/age-verify corruption detector + quarantine mover

- Prompts for directory to watch
- Monitors for new files appearing (e.g., PDFs being downloaded)
- Waits for each file to finish writing (size stable)
- Scans the header bytes for HTML markers (<html, <!doctype html, etc.)
- Prints: "{filename} checked - PASS"
- On fail:
    - prints "ERROR! CORRUPTED FILE DOWNLOADED!" 10 times + terminal bell
    - moves file into: <watch_dir>/quarantine/
    - pauses, prompts Enter to resume, continues where it left off
- Saves progress to watcher_state.json in the directory you run it from
"""

import os
import sys
import time
import json
from pathlib import Path
from datetime import datetime

# ===================== CONFIG =====================
POLL_INTERVAL_SEC = 1.0
STABLE_CHECKS = 3
STABLE_INTERVAL_SEC = 0.5
HEADER_READ_BYTES = 8192
STATE_FILENAME = "watcher_state.json"
QUARANTINE_DIRNAME = "quarantine"
# ==================================================

HTML_MARKERS = [
    b"<html",
    b"<!doctype html",
    b"<head",
    b"<title",
]

def now_ts() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def term_bell(times: int = 3):
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

def list_files_sorted(watch_dir: Path, quarantine_dir: Path):
    """
    Non-recursive listing of files in watch_dir, excluding quarantine folder contents.
    """
    files = []
    try:
        for entry in os.scandir(watch_dir):
            # Skip quarantine directory itself
            if entry.is_dir() and Path(entry.path).resolve() == quarantine_dir:
                continue
            if not entry.is_file():
                continue
            try:
                st = entry.stat()
            except FileNotFoundError:
                continue
            files.append((st.st_mtime, entry.name))
    except FileNotFoundError:
        return []
    files.sort(key=lambda x: (x[0], x[1]))
    return [name for _, name in files]

def wait_until_stable(file_path: Path) -> bool:
    last_size = None
    stable_count = 0

    while stable_count < STABLE_CHECKS:
        if not file_path.exists():
            return False
        try:
            size = file_path.stat().st_size
        except FileNotFoundError:
            return False

        if last_size is not None and size == last_size:
            stable_count += 1
        else:
            stable_count = 0

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

    # PDF signature check
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

def big_corruption_warning(file_name: str):
    msg = "ERROR! CORRUPTED FILE DOWNLOADED!"
    print("\n" + "=" * 70)
    for _ in range(10):
        print(msg, "->", file_name)
    print("=" * 70 + "\n")
    term_bell(times=8)

def move_to_quarantine(src: Path, quarantine_dir: Path) -> Path | None:
    """
    Move src file into quarantine_dir.
    If filename exists, auto-increment: name.ext -> name__q1.ext, name__q2.ext, ...
    Returns destination path, or None if move failed.
    """
    quarantine_dir.mkdir(parents=True, exist_ok=True)

    base = src.stem
    ext = src.suffix  # includes leading '.'
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
        src.replace(dest)  # atomic move on same filesystem
        return dest
    except Exception:
        # fallback: copy+delete if cross-device, but usually not needed here
        try:
            import shutil
            shutil.copy2(src, dest)
            src.unlink(missing_ok=True)
            return dest
        except Exception:
            return None

def main():
    print("=== Directory Watcher (HTML corruption detector + quarantine) ===")
    watch_dir_input = input("Directory to watch: ").strip().strip('"').strip("'")
    if not watch_dir_input:
        print("No directory entered. Exiting.")
        return

    watch_dir = Path(watch_dir_input).expanduser().resolve()
    if not watch_dir.exists() or not watch_dir.is_dir():
        print(f"Invalid directory: {watch_dir}")
        return

    quarantine_dir = (watch_dir / QUARANTINE_DIRNAME).resolve()

    state_path = Path.cwd() / STATE_FILENAME
    state = load_state(state_path)

    if state.get("watch_dir") != str(watch_dir):
        state = {"watch_dir": str(watch_dir), "last_index": 0, "processed": {}}
        save_state(state_path, state)

    processed: dict = state.get("processed", {})
    last_index = int(state.get("last_index", 0))

    print(f"[{now_ts()}] Watching: {watch_dir}")
    print(f"[{now_ts()}] Quarantine: {quarantine_dir}")
    print(f"[{now_ts()}] State: {state_path}")
    print("Press Ctrl+C to stop.\n")

    try:
        while True:
            files = list_files_sorted(watch_dir, quarantine_dir)

            if last_index > len(files):
                last_index = len(files)

            while last_index < len(files):
                name = files[last_index]
                file_path = (watch_dir / name).resolve()

                if processed.get(name):
                    last_index += 1
                    continue

                stable = wait_until_stable(file_path)
                if not stable:
                    processed[name] = True
                    last_index += 1
                    state["processed"] = processed
                    state["last_index"] = last_index
                    save_state(state_path, state)
                    continue

                is_bad = header_is_html(file_path)

                if is_bad:
                    big_corruption_warning(name)

                    dest = move_to_quarantine(file_path, quarantine_dir)
                    if dest:
                        print(f"[{now_ts()}] Moved to quarantine: {dest.name}")
                    else:
                        print(f"[{now_ts()}] WARNING: failed to move {name} to quarantine!")

                    processed[name] = True
                    last_index += 1
                    state["processed"] = processed
                    state["last_index"] = last_index
                    save_state(state_path, state)

                    input("Paused. Press Enter to resume... ")
                    print(f"[{now_ts()}] Resuming...\n")
                else:
                    print(f"{name} checked - PASS")
                    processed[name] = True
                    last_index += 1

                    state["processed"] = processed
                    state["last_index"] = last_index
                    save_state(state_path, state)

            time.sleep(POLL_INTERVAL_SEC)

    except KeyboardInterrupt:
        print("\nStopped by user (Ctrl+C). Saving state...")
        state["processed"] = processed
        state["last_index"] = last_index
        save_state(state_path, state)
        print(f"State saved to: {state_path}")

if __name__ == "__main__":
    main()
