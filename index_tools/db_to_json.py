#!/usr/bin/env python3
"""
db_to_ripper_json_ui.py

Convert SQLite DB index (our scanner) -> ripper-compatible index JSON.

Run from epstein project root (the folder containing data9/, data10/, etc).
No CLI args.

Input (expected):
  ./dataN/index_dataN.sqlite  (or .db/.sqlite3)
DB schema expected (minimum):
  files table with columns: filename, url, last_page (or last_page nullable), first_seen, last_seen
  Optionally has dataset_id column for filtering.

Output:
  ./dataN/index_dataN_from_db.json

Safe: does not modify DB. Writes a new JSON file.
"""

import os
import json
import sqlite3
from datetime import datetime
from typing import Dict, Any, Optional, List

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


def find_db_path(root: str, ds: int) -> Optional[str]:
    ddir = os.path.join(root, f"data{ds}")
    for name in (f"index_data{ds}.sqlite", f"index_data{ds}.db", f"index_data{ds}.sqlite3"):
        p = os.path.join(ddir, name)
        if os.path.isfile(p):
            return p
    return None


def atomic_write_json(path: str, data: Dict[str, Any]) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, sort_keys=True)
    os.replace(tmp, path)


def db_has_table(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    return row is not None


def db_has_column(conn: sqlite3.Connection, table: str, col: str) -> bool:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return any(r[1] == col for r in rows)  # PRAGMA table_info: (cid, name, type, notnull, dflt_value, pk)


def infer_dataset_id(conn: sqlite3.Connection) -> Optional[int]:
    if not db_has_table(conn, "meta"):
        return None
    try:
        row = conn.execute("SELECT value FROM meta WHERE key='dataset_id'").fetchone()
        if row and row[0] is not None:
            return int(row[0])
    except Exception:
        return None
    return None


def load_files_from_db(db_path: str, ds: int) -> List[sqlite3.Row]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        if not db_has_table(conn, "files"):
            raise RuntimeError("DB missing 'files' table.")

        has_ds = db_has_column(conn, "files", "dataset_id")
        effective_ds = ds

        # if DB has dataset_id column, filter; otherwise read all rows
        if has_ds:
            rows = conn.execute(
                "SELECT filename, url, first_seen, last_seen, last_page FROM files WHERE dataset_id=?",
                (effective_ds,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT filename, url, first_seen, last_seen, last_page FROM files"
            ).fetchall()

        return rows
    finally:
        conn.close()


def main() -> None:
    print("\n=== DB -> Ripper JSON Converter (UI) ===")
    root = os.getcwd()

    ds = prompt_dataset()
    db_path = find_db_path(root, ds)
    if not db_path:
        print("\nERROR: Could not find DB in expected location.")
        print(f"Looked for: ./data{ds}/index_data{ds}.sqlite (or .db/.sqlite3)")
        raise SystemExit(1)

    out_path = os.path.join(root, f"data{ds}", f"index_data{ds}_from_db.json")
    if os.path.exists(out_path):
        raw = input(f"\nOutput exists:\n  {out_path}\nOverwrite? [y/N]: ").strip().lower()
        if raw not in {"y", "yes"}:
            print("Canceled.")
            raise SystemExit(0)

    rows = load_files_from_db(db_path, ds)

    # Build ripper-compatible JSON
    idx: Dict[str, Any] = {
        "meta": {
            "dataset": ds,
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "last_scan_at": None,
            "last_scan_page": 0,
            "version": 2,
            "source": "sqlite_db_export",
            "source_db": os.path.abspath(db_path),
        },
        "files": {}
    }

    files = idx["files"]
    now = datetime.now().isoformat(timespec="seconds")

    for r in rows:
        filename = r["filename"]
        url = r["url"]
        first_seen = r["first_seen"] or now
        last_seen = r["last_seen"] or first_seen
        page = r["last_page"] if r["last_page"] is not None else 1

        # Ripper-compatible entry (safe defaults for downloader)
        files[filename] = {
            "url": url,
            "first_seen": first_seen,
            "last_seen": last_seen,
            "page": int(page) if isinstance(page, int) or str(page).isdigit() else 1,
            "downloaded": False,
            "downloaded_at": None,
            "sha256": None,
            "bytes": None,
            "attempts": 0,
            "last_error": None,
        }

    atomic_write_json(out_path, idx)

    print("\n=== DONE ===")
    print(f"DB:      {db_path}")
    print(f"Output:  {out_path}")
    print(f"Entries: {len(rows)}")
    print("This JSON is ripper-usable as a starter index (downloaded=false for all).")
    print("if you already have files in the dir run index_repair.py")
    print("if you have an old json in the dir, back it up/rename it, and rename this new json file as 'index_data#.json' - where number sign is , put the number of that data set, make sure its placed in the data#/ directory")
    print("============\n")


if __name__ == "__main__":
    main()
