#!/usr/bin/env python3
"""
reset_index_public_ui.py

No CLI args. Fully interactive.

What it does:
- Prompts for dataset (1-12)
- Auto-finds:   ./dataN/index_dataN.json   (relative to where you RUN the script)
- Loads JSON, duplicates it, and resets ALL file entries to:
    downloaded = False
    downloaded_at = None
    sha256 = None
    bytes = None
    attempts = 0
    last_error = None
- Saves the cloned "public" index into the *parent dir where you ran the script* (project root),
  NOT inside dataN/.

Output file name (in project root):
    index_dataN_public.json

Safe: never overwrites the original index_dataN.json
"""

import os
import json
from datetime import datetime
from typing import Any, Dict

DATASET_RANGE = range(1, 13)


def prompt_dataset() -> int:
    print("\nAvailable datasets:", ", ".join(str(n) for n in DATASET_RANGE))
    while True:
        raw = input("Pick dataset (1-12): ").strip()
        try:
            n = int(raw)
            if n in DATASET_RANGE:
                return n
        except ValueError:
            pass
        print("Invalid dataset. Try again.")


def load_json(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def atomic_save_json(path: str, data: Dict[str, Any]) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, sort_keys=True)
    os.replace(tmp, path)


def reset_entry(entry: Dict[str, Any]) -> None:
    entry["downloaded"] = False
    entry["downloaded_at"] = None
    entry["sha256"] = None
    entry["bytes"] = None
    entry["attempts"] = 0
    entry["last_error"] = None


def main() -> None:
    print("\n=== Public Index Builder (no-args UI) ===")
    project_root = os.getcwd()  # where you RUN it from (matches your toolchain convention)

    ds = prompt_dataset()

    in_path = os.path.join(project_root, f"data{ds}", f"index_data{ds}.json")
    out_path = os.path.join(project_root, f"index_data{ds}_public.json")

    if not os.path.isfile(in_path):
        print("\nERROR: Expected JSON index not found.")
        print(f"Looked for: {in_path}")
        print("\nFix:")
        print(" - Run this script from your epstein project root (the folder that contains data9/, data10/, etc).")
        raise SystemExit(1)

    if os.path.exists(out_path):
        raw = input(f"\nOutput already exists:\n  {out_path}\nOverwrite it? [y/N]: ").strip().lower()
        if raw not in {"y", "yes"}:
            print("Canceled.")
            raise SystemExit(0)

    data = load_json(in_path)

    if "files" not in data or not isinstance(data["files"], dict):
        print("\nERROR: JSON does not look like a ripper index (missing top-level 'files' dict).")
        raise SystemExit(1)

    files = data["files"]
    reset_count = 0

    for _, entry in files.items():
        if isinstance(entry, dict):
            reset_entry(entry)
            reset_count += 1

    # Stamp meta so you can tell it's a public export
    meta = data.get("meta")
    if not isinstance(meta, dict):
        meta = {}
        data["meta"] = meta
    meta["public_export"] = True
    meta["public_export_at"] = datetime.now().isoformat(timespec="seconds")

    atomic_save_json(out_path, data)

    print("\n=== DONE ===")
    print(f"Input:  {in_path}")
    print(f"Output: {out_path}")
    print(f"Entries reset: {reset_count}")
    print("Original file was NOT modified.")
    print("============\n")


if __name__ == "__main__":
    main()
