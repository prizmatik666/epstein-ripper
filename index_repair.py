#!/usr/bin/env python3
import os
import json
import shutil
from datetime import datetime

DATASET_RANGE = range(1, 13)


def bytes_look_like_pdf(path):
    try:
        with open(path, "rb") as f:
            head = f.read(5)
        return head == b"%PDF-"
    except Exception:
        return False


def backup_index(index_path):
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = f"{index_path}.{ts}.bak"

    # Pure content copy (no metadata operations)
    with open(index_path, "rb") as src:
        data = src.read()

    with open(backup_path, "wb") as dst:
        dst.write(data)

    return backup_path

def ask_dataset():
    print("\nAvailable datasets:")
    print(",".join(str(n) for n in DATASET_RANGE))

    while True:
        choice = input("\nEnter dataset number to repair: ").strip()
        if choice.isdigit():
            n = int(choice)
            if n in DATASET_RANGE:
                return n
        print("Invalid selection. Try again.")


def repair_index(index_path, dataset_dir):
    with open(index_path, "r", encoding="utf-8") as f:
        idx = json.load(f)

    files = idx.get("files", {})
    total_entries = len(files)

    print("\nStarting index integrity scan...\n")

    missing_files = 0
    non_pdf_files = 0
    repaired = 0
    already_clean = 0

    for filename, entry in files.items():

        if not entry.get("downloaded", False):
            continue

        file_path = os.path.join(dataset_dir, filename)

        print(f"[CHECK] {filename}")

        # Case 1: Missing file
        if not os.path.exists(file_path):
            missing_files += 1
            repaired += 1
            entry["downloaded"] = False
            entry["downloaded_at"] = None
            print(f"  ΓåÆ MISSING on disk. Resetting downloaded=False")
            continue

        # Case 2: Exists but not real PDF
        if not bytes_look_like_pdf(file_path):
            non_pdf_files += 1
            repaired += 1
            entry["downloaded"] = False
            entry["downloaded_at"] = None
            print(f"  ΓåÆ NOT A VALID PDF. Resetting downloaded=False")
            continue

        already_clean += 1
        print(f"  ΓåÆ OK")

    # Save repaired index
    with open(index_path, "w", encoding="utf-8") as f:
        json.dump(idx, f, indent=2, sort_keys=True)

    print("\n" + "=" * 70)
    print("INDEX REPAIR REPORT")
    print("=" * 70)
    print(f"Index file:              {index_path}")
    print(f"Dataset directory:       {dataset_dir}")
    print(f"Total index entries:     {total_entries}")
    print(f"Already valid entries:   {already_clean}")
    print(f"Missing files found:     {missing_files}")
    print(f"Non-PDF files found:     {non_pdf_files}")
    print(f"Total repairs applied:   {repaired}")
    print("=" * 70 + "\n")


def main():
    dataset = ask_dataset()

    dataset_dir = f"data{dataset}"
    index_path = os.path.join(dataset_dir, f"index_data{dataset}.json")

    if not os.path.isdir(dataset_dir):
        print(f"\nDataset directory not found: {dataset_dir}")
        return

    if not os.path.exists(index_path):
        print(f"\nIndex file not found: {index_path}")
        return

    print(f"\nSelected dataset: {dataset}")
    print(f"Directory: {dataset_dir}")
    print(f"Index file: {index_path}")

    confirm = input("\nProceed with repair scan? (y/n): ").strip().lower()
    if confirm != "y":
        print("Aborted.")
        return

    backup_path = backup_index(index_path)
    print(f"\nBackup created: {backup_path}")

    repair_index(index_path, dataset_dir)


if __name__ == "__main__":
    main()
