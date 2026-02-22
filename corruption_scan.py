#!/usr/bin/env python3
"""
Standalone Directory Corruption Scanner

- Prompts for directory
- Scans all files (non-recursive)
- Detects HTML / age-verification corruption
- Moves corrupted files into: <dir>/quarantine/
- Auto-renames on filename collision
"""

import os
from pathlib import Path
from datetime import datetime

# ================= CONFIG =================
HEADER_READ_BYTES = 8192
QUARANTINE_DIRNAME = "quarantine"
# ==========================================

HTML_MARKERS = [
    b"<html",
    b"<!doctype html",
    b"<head",
    b"<title",
]

def now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def header_is_html(file_path: Path) -> bool:
    try:
        with file_path.open("rb") as f:
            head = f.read(HEADER_READ_BYTES)
    except Exception:
        return True  # unreadable = suspicious

    if not head:
        return True

    # Proper PDF signature
    if head.startswith(b"%PDF-"):
        return False

    head_lc = head.lower()

    for marker in HTML_MARKERS:
        if marker in head_lc:
            return True

    # Extra heuristic
    if head[:1] == b"<":
        if b"</" in head_lc or b"document" in head_lc or b"script" in head_lc:
            return True

    return False

def move_to_quarantine(src: Path, quarantine_dir: Path):
    quarantine_dir.mkdir(exist_ok=True)

    base = src.stem
    ext = src.suffix
    dest = quarantine_dir / (base + ext)

    if dest.exists():
        i = 1
        while True:
            candidate = quarantine_dir / f"{base}__q{i}{ext}"
            if not candidate.exists():
                dest = candidate
                break
            i += 1

    try:
        src.replace(dest)
        return dest
    except Exception:
        import shutil
        try:
            shutil.copy2(src, dest)
            src.unlink(missing_ok=True)
            return dest
        except Exception:
            return None

def main():
    print("=== Standalone Corruption Scanner ===")
    scan_input = input("Directory to scan: ").strip().strip('"').strip("'")

    if not scan_input:
        print("No directory entered. Exiting.")
        return

    scan_dir = Path(scan_input).expanduser().resolve()

    if not scan_dir.exists() or not scan_dir.is_dir():
        print("Invalid directory.")
        return

    quarantine_dir = scan_dir / QUARANTINE_DIRNAME

    total = 0
    corrupted = 0

    print(f"\n[{now()}] Scanning: {scan_dir}\n")

    for entry in os.scandir(scan_dir):
        if entry.is_dir():
            if entry.name == QUARANTINE_DIRNAME:
                continue
            continue

        file_path = Path(entry.path)
        total += 1

        is_bad = header_is_html(file_path)

        if is_bad:
            dest = move_to_quarantine(file_path, quarantine_dir)
            corrupted += 1
            if dest:
                print(f"[CORRUPTED] {file_path.name} -> moved to quarantine")
            else:
                print(f"[ERROR] Could not move {file_path.name}")
        else:
            print(f"[PASS] {file_path.name}")

    print("\n=======================================")
    print(f"Files scanned : {total}")
    print(f"Corrupted     : {corrupted}")
    print(f"Clean         : {total - corrupted}")
    print("=======================================\n")

if __name__ == "__main__":
    main()