#!/usr/bin/env python3
import os
import json
from datetime import datetime

DATASET_RANGE = range(1, 12)


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

    # Buckets based on correctness definition:
    # A) downloaded=True  + file exists + valid PDF          -> valid
    # B) downloaded=True  + (missing OR not PDF)             -> invalid, flip to False
    # C) downloaded=False + file missing                     -> valid
    # D) downloaded=False + file exists + valid PDF          -> invalid, flip to True
    # E) downloaded=False + file exists + not PDF            -> valid (not downloaded; file is junk)

    valid_A = 0
    invalid_B = 0
    valid_C = 0
    invalid_D = 0
    valid_E = 0

    flipped_to_true = 0
    flipped_to_false = 0

    non_pdf_files_on_disk = 0
    missing_on_disk = 0

    for filename, entry in files.items():
        if not isinstance(entry, dict):
            continue

        file_path = os.path.join(dataset_dir, filename)

        idx_downloaded = bool(entry.get("downloaded", False))
        exists = os.path.exists(file_path)

        if not exists:
            missing_on_disk += 1
            if idx_downloaded:
                # Case B
                invalid_B += 1
                entry["downloaded"] = False
                entry["downloaded_at"] = None
                entry["bytes"] = None
                entry["sha256"] = None
                flipped_to_false += 1
            else:
                # Case C
                valid_C += 1
            continue

        # exists == True
        is_pdf = bytes_look_like_pdf(file_path)
        if not is_pdf:
            non_pdf_files_on_disk += 1
            if idx_downloaded:
                # downloaded=True but file isn't a PDF -> treat as invalid, flip to False
                invalid_B += 1
                entry["downloaded"] = False
                entry["downloaded_at"] = None
                entry["bytes"] = None
                entry["sha256"] = None
                flipped_to_false += 1
            else:
                # downloaded=False and file is not a PDF -> valid (it's not a successful download)
                valid_E += 1
            continue

        # exists and is valid PDF
        if idx_downloaded:
            # Case A
            valid_A += 1
            if entry.get("bytes") is None:
                try:
                    entry["bytes"] = os.path.getsize(file_path)
                except Exception:
                    pass
        else:
            # Case D
            invalid_D += 1
            entry["downloaded"] = True
            entry["downloaded_at"] = entry.get("downloaded_at") or datetime.now().isoformat(timespec="seconds")
            entry["last_error"] = None
            try:
                entry["bytes"] = os.path.getsize(file_path)
            except Exception:
                pass
            flipped_to_true += 1

    # Save repaired index
    with open(index_path, "w", encoding="utf-8") as f:
        json.dump(idx, f, indent=2, sort_keys=True)

    valid_total = valid_A + valid_C + valid_E
    invalid_total = invalid_B + invalid_D
    repairs_applied = flipped_to_true + flipped_to_false

    print("\n" + "=" * 70)
    print("INDEX REPAIR REPORT")
    print("=" * 70)
    print(f"Index file:                         {index_path}")
    print(f"Dataset directory:                  {dataset_dir}")
    print(f"Total index entries:                {total_entries}")
    print("")
    print("Correctness buckets:")
    print(f"  A) downloaded=True  & PDF exists: {valid_A}   (valid)")
    print(f"  B) downloaded=True  but missing/non-PDF: {invalid_B}   (invalid)")
    print(f"  C) downloaded=False & missing:    {valid_C}   (valid)")
    print(f"  D) downloaded=False but PDF exists: {invalid_D}   (invalid)")
    print(f"  E) downloaded=False & non-PDF exists: {valid_E}   (valid)")
    print("")
    print(f"Valid entries total:                {valid_total}")
    print(f"Invalid entries total:              {invalid_total}")
    print("")
    print("Disk observations:")
    print(f"  Missing on disk:                  {missing_on_disk}")
    print(f"  Non-PDF files on disk:            {non_pdf_files_on_disk}")
    print("")
    print("Repairs applied:")
    print(f"  Flipped -> downloaded=True:       {flipped_to_true}")
    print(f"  Flipped -> downloaded=False:      {flipped_to_false}")
    print(f"  Total repairs applied:            {repairs_applied}")
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