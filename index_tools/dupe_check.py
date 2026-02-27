#!/usr/bin/env python3
"""
check_index_duplicates_ui.py

No CLI args. Run from your epstein project root.

What it does:
- Prompts for dataset (1-12)
- Loads: ./dataN/index_dataN.json
- Checks for "duplicate entries" in the ways that actually matter for your ripper index:

  1) Duplicate KEYS in JSON? (JSON parsers can't represent duplicate keys; they'd be overwritten already.)
     -> So you cannot detect "duplicate keys" after the fact unless you parse raw JSON text.

  2) Duplicate file_num collisions:
     - Extracts numeric ID from filenames like EFTA02241636.pdf
     - Reports if multiple filenames map to the same number (rare but important)

  3) Duplicate URL collisions:
     - Reports if the same URL appears for multiple filenames

  4) Same filename but conflicting metadata can't happen (dict key), but we still sanity-check entries.

Outputs (in project root):
  out_dupecheck_dsN/
    dup_file_nums.txt
    dup_urls.txt
    summary.txt

Safe: read-only.
"""

import os
import re
import json
from collections import defaultdict
from datetime import datetime
from typing import Dict, Any, List, Tuple

DATASET_RANGE = range(1, 13)
EFTA_RE = re.compile(r"^EFTA0*(\d+)\.pdf$", re.IGNORECASE)


def ts() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


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


def extract_file_num(filename: str):
    m = EFTA_RE.match(filename)
    if not m:
        return None
    try:
        return int(m.group(1))
    except ValueError:
        return None


def load_index(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def write_lines(path: str, lines: List[str]) -> None:
    with open(path, "w", encoding="utf-8") as f:
        for ln in lines:
            f.write(ln + "\n")


def main() -> None:
    print("\n=== Index Duplicate Checker (UI) ===")
    root = os.getcwd()

    ds = prompt_dataset()
    in_path = os.path.join(root, f"data{ds}", f"index_data{ds}.json")

    if not os.path.isfile(in_path):
        print("\nERROR: Expected JSON index not found.")
        print(f"Looked for: {in_path}")
        print("\nFix: run from your epstein project root (contains data9/, data10/, etc).")
        raise SystemExit(1)

    data = load_index(in_path)
    files = data.get("files")

    if not isinstance(files, dict):
        print("\nERROR: JSON does not look like ripper index (missing top-level 'files' dict).")
        raise SystemExit(1)

    # Collisions
    by_num = defaultdict(list)   # file_num -> [filename...]
    by_url = defaultdict(list)   # url -> [filename...]

    total = 0
    efta_total = 0
    missing_url = 0
    non_dict_entries = 0

    for fname, entry in files.items():
        total += 1
        if not isinstance(entry, dict):
            non_dict_entries += 1
            continue

        url = entry.get("url")
        if not isinstance(url, str) or not url.strip():
            missing_url += 1
        else:
            by_url[url].append(fname)

        n = extract_file_num(fname)
        if n is not None:
            efta_total += 1
            by_num[n].append(fname)

    dup_nums = {n: names for n, names in by_num.items() if len(names) > 1}
    dup_urls = {u: names for u, names in by_url.items() if len(names) > 1}

    outdir = os.path.join(root, f"out_dupecheck_ds{ds}")
    os.makedirs(outdir, exist_ok=True)

    dup_nums_path = os.path.join(outdir, "dup_file_nums.txt")
    dup_urls_path = os.path.join(outdir, "dup_urls.txt")
    summary_path = os.path.join(outdir, "summary.txt")

    # Build output lines
    dup_num_lines: List[str] = []
    for n in sorted(dup_nums.keys()):
        names = sorted(dup_nums[n])
        dup_num_lines.append(f"FILE_NUM {n} -> {len(names)} filenames")
        for fn in names:
            dup_num_lines.append(f"  - {fn}")
        dup_num_lines.append("")

    dup_url_lines: List[str] = []
    # Keep url report compact; urls can be long
    for u in sorted(dup_urls.keys()):
        names = sorted(dup_urls[u])
        dup_url_lines.append(f"URL -> {len(names)} filenames")
        dup_url_lines.append(f"  {u}")
        for fn in names:
            dup_url_lines.append(f"  - {fn}")
        dup_url_lines.append("")

    summary_lines = [
        "=== Duplicate Check Summary ===",
        f"Generated: {ts()}",
        f"Dataset: {ds}",
        f"Index: {in_path}",
        "",
        f"Total entries in files dict: {total}",
        f"EFTA-style filenames detected: {efta_total}",
        f"Entries with missing/blank url: {missing_url}",
        f"Non-dict entries under files: {non_dict_entries}",
        "",
        "IMPORTANT NOTE:",
        "  JSON cannot preserve duplicate keys. If the JSON ever had duplicate filenames as keys,",
        "  only the last one would survive when the file was written/loaded.",
        "",
        f"Duplicate file_num collisions: {len(dup_nums)}",
        f"Duplicate URL collisions: {len(dup_urls)}",
        "",
        "Files written:",
        f"  {dup_nums_path}",
        f"  {dup_urls_path}",
        f"  {summary_path}",
    ]

    write_lines(dup_nums_path, dup_num_lines if dup_num_lines else ["(none)"])
    write_lines(dup_urls_path, dup_url_lines if dup_url_lines else ["(none)"])
    write_lines(summary_path, summary_lines)

    print("\n=== DONE ===")
    print(f"Total entries: {total} (EFTA detected: {efta_total})")
    print(f"Duplicate file_num collisions: {len(dup_nums)}")
    print(f"Duplicate URL collisions: {len(dup_urls)}")
    print(f"Missing URL entries: {missing_url}")
    print(f"Output folder: {outdir}")
    print("============\n")


if __name__ == "__main__":
    main()
