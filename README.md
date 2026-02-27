# EPSTEIN-RIPPER

Reliable, resumable archival downloader and validator for DOJ Epstein
disclosure datasets.

------------------------------------------------------------------------

## Overview

`epstein-ripper` is a resilient browser-driven crawler and downloader
designed to archive publicly released Epstein document datasets hosted
by the U.S. Department of Justice.

The DOJ interface presents multiple challenges:

-   Pagination that repeats or remixes pages
-   No reliable "last page" indicator
-   Short-lived authorization cookies
-   Anti-automation challenges
-   Occasional HTML responses served as `.pdf` files

This tool prioritizes reliability, integrity, and safe resume behavior, while striving to be user friendl.
During the pursuit of establishing consistent and accurate index's of the DOJ's file lists i've found manyobstacles. I've done my best to defeat them to accomplish this goal, and to share with you. 

Please leave a star, watch, or fork to help spread this software to those who may use it. Thank you for reading, cloning, using, etc ! 

The pursuit of truth, justice, and .pdf punishment is imperative. We're all a tool for change.
            - Prizm 

------------------------------------------------------------------------

## Quick Start

``` bash
git clone https://github.com/prizmatik666/epstein-ripper
cd epstein-ripper
pip install -r requirements.txt
playwright install chromium
python epstein_ripper.py
```

You will be prompted for:

-   Dataset selection
-   Operating mode (sync / scan / download)

------------------------------------------------------------------------

## Core Features

### Dataset Selection

Choose individual datasets or ranges:

    1,3,5
    1-11
    9-11

### Dynamic Page Detection

Pages are scanned until no new PDFs appear for a defined threshold.\
Pagination behavior from DOJ is unpredictable --- this system adapts.

### Persistent Scan Index

Each dataset maintains its own index:

    dataX/index_dataX.json

The index tracks:

-   Discovered PDFs
-   Source page numbers
-   Download status
-   Retry counts
-   Timestamps

This enables:

-   Crash-safe resume
-   Missing file repair
-   Safe re-walk scanning
-   Update detection

### Crash-Safe Downloads

Files download to:

    filename.pdf.part

They are renamed only after validation completes.\
This prevents partial or corrupted files from being marked complete.

### Session Protection

If DOJ returns HTML instead of a real PDF:

-   File is NOT written
-   A visible alert is triggered
-   Download pauses
-   User re-authenticates
-   Fresh context is created
-   File is safely retried

Normal HTTP errors do not trigger re-authentication.

------------------------------------------------------------------------

## Operating Modes

  Mode         Behavior
  ------------ ---------------------------------------------
  `sync`       Scan + download missing files (recommended)
  `scan`       Scan only, update index
  `download`   Download missing files using existing index

------------------------------------------------------------------------

## Output Structure

Example:

    data9/
        EFTA00012345.pdf
        index_data9.json

    resume_data9.txt
    download.log

Files:

-   PDFs --- Downloaded documents
-   `index_dataX.json` --- Scan index
-   `resume_dataX.txt` --- Last scanned page
-   `download.log` --- Activity log

Do not rename or delete files while the script is running.

------------------------------------------------------------------------

## Data Integrity Notes

### Updated index_repair.py (2/26/2026)

The upgraded repair utility:

-   Correctly flips `downloaded=False ΓåÆ True` when files exist
-   Correctly flips `downloaded=True ΓåÆ False` when missing
-   Provides structured integrity reporting

Use the updated version.

### Pagination Warning (2/25/2026)

DOJ pagination can repeat page results far beyond actual dataset depth.\
Short "no new page" thresholds are unsafe.

The default stop threshold was increased significantly after real-world
testing revealed new PDFs appearing thousands of pages later.

If performing deep archival scans, use a high no-new threshold.

### Validation Required for Older Downloads

Older versions may have saved HTML as PDFs due to upstream behavior.

If you downloaded datasets before the validation upgrade:

-   Run integrity utilities
-   Validate file signatures
-   Perform a repair pass

------------------------------------------------------------------------

## Main Utilities

Optional but recommended tools are included for dataset validation and
analysis.

### active_watcher.py

Real-time corruption detection while downloading.

-   Monitors dataset directory
-   Validates PDF headers
-   Quarantines corrupted files
-   Logs quarantine events
-   Pauses with visible alert until acknowledged

- this was included as a temporary fix utility while
- a fix was being implemented for the 'html-served-as-.pdf' bug
- no longer needed to be utilized during downloads as the check
- happens inside the ripper now before saving to disk.

------------------------------------------------------------------------

### corruption_scan.py

One-time sweep utility.

-   Scans a directory for corrupted files
-   Validates `%PDF-` signature
-   Detects HTML markers
-   Moves corrupted files to `quarantine/`
-   Prints summary report
-   If files are removed from a dataset, run index_repair to flip the 
    download= value back to false in the index

Safe to run multiple times.

------------------------------------------------------------------------

### index_repair.py

Index reconciliation tool.

-   Creates `.bak` backup of index
-   Validates disk vs index state
-   Repairs mismatches(downloaded=True/False)
-   Reports correctness buckets
-   Safe to rerun
-   After running corruptions_scan on a dataset - if it removes files
    run index_repair on that datasets index to flip the downloaded value
    back to false
------------------------------------------------------------------------

### image_ripper.py

Bulk embedded image extractor (GUI).

Extracts embedded images from large PDF collections.

Features:

-   Recursive folder scanning
-   Incremental re-run support
-   Process tracking via `processed_pdfs.txt`
-   Image mapping log (`image_map.txt`)

Requirements:

    pip install pymupdf pillow

Designed for:

-   Large disclosure datasets
-   Forensic review
-   Visual content isolation

------------------------------------------------------------------------

## Requirements

-   Python 3.9+
-   Playwright
-   Chromium browser (installed via Playwright)

```{=html}
<!-- -->
```
    pip install playwright
    playwright install chromium

------------------------------------------------------------------------

## index_files/
------------------------------------------------------------------------
 - This is where I include index_data#.json file's that i've made through scanning the datasets. 
 - If you wish to use mine instead of scanning and building your own index - move the .json for the dataset your working with into it's data#/ directory, named as index_data#.json exactly (where # = dataset number)
 - If you already have downloads in your data#/ when deciding to try one of my index files- run index_repair.py on it before downloading again. it will set the files you have on disk to downloaded=True in the index , so theyre not downloaded again.
- Scans to build full indexes on these massive datasets take a LONG time. I will be uploading them as I get them ready.

## index_tools/
------------------------------------------------------------------------
I'm working with doing scanning with a util that scans and uses a sqlte database for the index file instead of .json

using sql/db files for the download index in the ripper is not currently supported - but will probably add in that option later. 

mostly i'm experimenting with:
- speed and reliability of the sql scan vs. the built in ripper -> .json scanner for making the index file.
- how the DOJ site behaves as far as serving duplicate file list pages in higher # page's in the various datasets
- data9 started having alot of trouble after page 1000
- db scanner couldnt break out of the 'same file list' loop that was happening
- the built in ripper scan had the same problem but would eventually break out of a no-new streak. It had high value   streaks: more than 100,200 no-new-pdf's in a row before breaking out and returning new filenames. 
-i ran my data9 scan with max no new @ 300. 
```
[2026-02-25 21:58:57] [DS 9] No NEW PDFs on page 7990 (streak=300/300)
[2026-02-25 21:59:00] [DS 9] Stopping scan: no new PDFs for 300 consecutive pages.
[2026-02-25 21:59:14] === DATASET 9 COMPLETE ===
[2026-02-25 21:59:58] ALL DATASETS COMPLETE 
```
- doj's pagination makes knowing if your dataset file list is complete, but with 300 as the end count for no-new , you can have a much higher confidence that you scanned everything.
  
I will be trying to find the fastest, most reliable, and above all ACURATE - way of indexing the file names for download. I thought it would be good to include those tools here now to make updates easier - and for others to play around with.

- index_tools contains:
- db_index.py -> the page scanner to build file index w/ database
- db_to_json.py -> converts a db_index scan file into a ripper useable .json for downloading
- dupe_check.py -> checks a .json index for duplicate entries
- dupe_index.py -> duplicates a .json index and flips all download= values to false - making it into a fresh runable copy to be shared or freshly ran for download.
 - will make it useable on db files when db functionality is adopted in the main program also

## Support
------------------------------------------------------------------------
This project is developed and maintained independently by Prizm
(Prizmatik Underground).

If this tool has been useful to you, consider supporting future
development and research:

PayPal Donation Link:
https://www.paypal.com/ncp/payment/VVDAXZGKPQZKW

PayPal Email:
prizmatikug@gmail.com

Original Repository:
https://github.com/prizmatik666/epstein-ripper

Thank you for supporting independent open-source tools.

If you don't want to donate thats totally fine, but please, if you found this helpful- star the repo :) thank you!!

Death to .PDF's

------------------------------------------------------------------------

## Disclaimer

This tool accesses publicly available DOJ materials.
It does not bypass authentication or security controls.
All verification steps require explicit human interaction.
Provided for archival, research, and transparency purposes.
Use responsibly and in accordance with applicable laws and site terms.

