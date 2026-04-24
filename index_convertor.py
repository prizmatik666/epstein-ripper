#!/usr/bin/env python3
"""
codex_indexflip.py

Interactive converter for EpRip index formats.

Supports:
  - JSON -> SQLite
  - SQLite/DB -> JSON

Run from the epstein project root.
The source file is never modified. A new converted file is written.
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple


DATASET_RANGE = range(1, 13)


def prompt_dataset() -> int:
    print("\nAvailable datasets:", ", ".join(str(n) for n in DATASET_RANGE))
    while True:
        raw = input("Pick dataset (1-12): ").strip()
        try:
            ds = int(raw)
        except ValueError:
            ds = 0
        if ds in DATASET_RANGE:
            return ds
        print("Invalid dataset. Try again.")


def dataset_dir(root: str, ds: int) -> str:
    return os.path.join(root, f"data{ds}")


def discover_index_files(root: str, ds: int) -> Dict[str, List[str]]:
    ddir = dataset_dir(root, ds)
    found = {"json": [], "sqlite": []}
    if not os.path.isdir(ddir):
        return found

    prefix = f"index_data{ds}"
    for name in sorted(os.listdir(ddir)):
        if not name.startswith(prefix):
            continue
        path = os.path.join(ddir, name)
        if not os.path.isfile(path):
            continue
        lower = name.lower()
        if lower.endswith(".json"):
            found["json"].append(path)
        elif lower.endswith(".sqlite") or lower.endswith(".db") or lower.endswith(".sqlite3"):
            found["sqlite"].append(path)
    return found


def prompt_choice(title: str, options: List[str], default_idx: int = 0) -> str:
    print(f"\n{title}")
    for i, opt in enumerate(options, start=1):
        print(f"  {i}) {opt}")

    default_num = default_idx + 1
    raw = input(f"Choose [{default_num}]: ").strip()
    try:
        chosen = int(raw) if raw else default_num
    except ValueError:
        chosen = default_num
    if not 1 <= chosen <= len(options):
        chosen = default_num
    return options[chosen - 1]


def prompt_direction(found: Dict[str, List[str]]) -> str:
    json_count = len(found["json"])
    sqlite_count = len(found["sqlite"])
    print("\n=== Index Flip ===")
    print(f"JSON files found:   {json_count}")
    print(f"SQLite files found: {sqlite_count}")

    choices = []
    if json_count:
        choices.append("json_to_sqlite")
    if sqlite_count:
        choices.append("sqlite_to_json")
    if not choices:
        choices = ["json_to_sqlite", "sqlite_to_json"]

    labels = {
        "json_to_sqlite": "JSON -> SQLite",
        "sqlite_to_json": "SQLite -> JSON",
    }
    selected = prompt_choice("Choose conversion direction:", [labels[c] for c in choices], 0)
    for key, label in labels.items():
        if label == selected:
            return key
    return choices[0]


def prompt_path_from_candidates(title: str, paths: List[str]) -> str:
    if not paths:
        raise RuntimeError("No candidate files available.")
    names = [os.path.basename(p) for p in paths]
    selected_name = prompt_choice(title, names, 0)
    for path in paths:
        if os.path.basename(path) == selected_name:
            return path
    return paths[0]


def atomic_write_json(path: str, data: Dict[str, Any]) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, sort_keys=True)
    os.replace(tmp, path)


def ensure_sqlite_schema(conn: sqlite3.Connection) -> None:
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


def load_json_index(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict):
        raise RuntimeError(f"Top-level JSON object expected in {path}")
    data.setdefault("meta", {})
    data.setdefault("files", {})
    if not isinstance(data["files"], dict):
        raise RuntimeError(f"'files' object expected in {path}")
    return data


def load_sqlite_index(path: str) -> Dict[str, Any]:
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
            files[filename] = entry

        return {"meta": meta, "files": files}
    finally:
        conn.close()


def write_sqlite_index(path: str, idx: Dict[str, Any]) -> None:
    tmp = path + ".tmp"
    if os.path.exists(tmp):
        os.remove(tmp)

    conn = sqlite3.connect(tmp)
    try:
        ensure_sqlite_schema(conn)

        meta = idx.get("meta", {})
        conn.executemany(
            "INSERT INTO meta(key, value_json) VALUES (?, ?)",
            [(key, json.dumps(value, sort_keys=True)) for key, value in meta.items()],
        )

        rows: List[Tuple[Any, ...]] = []
        for filename, entry in idx.get("files", {}).items():
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
                conn.executemany(
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
            conn.executemany(
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


def default_output_path(source_path: str, direction: str) -> str:
    root, _ext = os.path.splitext(source_path)
    if direction == "json_to_sqlite":
        return root + "_from_json.sqlite"
    return root + "_from_sqlite.json"


def prompt_output_path(default_path: str) -> str:
    raw = input(f"Output path [{default_path}]: ").strip()
    return raw or default_path


def confirm_overwrite(path: str) -> None:
    if not os.path.exists(path):
        return
    raw = input(f"Output exists:\n  {path}\nOverwrite? [y/N]: ").strip().lower()
    if raw not in {"y", "yes"}:
        print("Canceled.")
        raise SystemExit(0)


def choose_source_path(direction: str, found: Dict[str, List[str]]) -> str:
    if direction == "json_to_sqlite":
        return prompt_path_from_candidates("Choose source JSON:", found["json"])
    return prompt_path_from_candidates("Choose source SQLite:", found["sqlite"])


def main() -> None:
    print("\n=== Codex Index Flip ===")
    root = os.getcwd()
    ds = prompt_dataset()
    found = discover_index_files(root, ds)
    direction = prompt_direction(found)
    source_path = choose_source_path(direction, found)
    out_path = prompt_output_path(default_output_path(source_path, direction))
    confirm_overwrite(out_path)

    if direction == "json_to_sqlite":
        idx = load_json_index(source_path)
        idx.setdefault("meta", {})
        idx["meta"].setdefault("dataset", ds)
        idx["meta"].setdefault("source", "json_export")
        idx["meta"]["source_json"] = os.path.abspath(source_path)
        write_sqlite_index(out_path, idx)
    else:
        idx = load_sqlite_index(source_path)
        idx.setdefault("meta", {})
        idx["meta"].setdefault("dataset", ds)
        idx["meta"].setdefault("source", "sqlite_export")
        idx["meta"]["source_db"] = os.path.abspath(source_path)
        atomic_write_json(out_path, idx)

    print("\n=== DONE ===")
    print(f"Dataset: {ds}")
    print(f"Source:  {source_path}")
    print(f"Output:  {out_path}")
    print(f"Entries: {len(idx.get('files', {}))}")
    print("The source file was not modified.")
    print("============\n")


if __name__ == "__main__":
    main()
