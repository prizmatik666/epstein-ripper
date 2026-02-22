EPSTEIN-RIPPER
-----------------------------------------------------
# ⚠️⚠️⚠️ IMPORTANT DATA INTEGRITY NOTICE ⚠️⚠️⚠️ #
-----------------------------------------------------
# ADDED: 2/22/2026 ⚠️ PLEASE SCAN YOUR DATASETS ⚠️ #
-----------------------------------------------------
Previous versions of this project may have downloaded corrupted or invalid PDF files 
due to upstream response behavior on justice.gov (HTML verification pages returned 
with .pdf extensions).

This can result in:
- Large numbers of corrupted files
- HTML content saved as PDF
- Incomplete or invalid datasets

If you previously downloaded datasets using earlier versions of this tool,
you MUST validate your files.

I thought it best to make these utils instead of altering the core 
code of the ripper.

New utilities have been added to detect and repair affected datasets.
See the **Utilities** section below for instructions.

active_watcher.py
Monitors a target dataset directory in real time and reports file additions, modifications, and deletions. This is useful during large download or repair operations to confirm activity, detect stalled processes, and observe unexpected file behavior without manually refreshing folders.

corruption_scan.py
Scans downloaded PDF datasets for integrity issues such as invalid file structures, HTML verification pages saved as .pdf, zero-byte files, or truncated downloads. Generates a report of corrupted or suspicious files so they can be selectively re-downloaded and repaired.

[ i had discovered while developing a search utility that DOJ had served me
 around 80k+ corrupted documents. Apparently the session cookie expired (or
 other similar behaviour) and resulted in the 'age verification' html page being saved as the intended .pdf ! I thought it was because i had set the sleep value to 0.00 - but uncovered, by using the active_watcher that after a few hours this behaviour repeated itself even with a proper sleep value set in the ripper ])
----------------------------------------------------
# ⚠️⚠️⚠️ END CRITICAL UPDATE / WARNING ⚠️⚠️⚠️   #

{ Reliable downloader and archival tool for DOJ Epstein dataset PDFs. }
  ----------
  OVERVIEW
  ----------

epstein-ripper is a resilient crawler and downloader designed to archive
the publicly released Epstein document datasets hosted on the U.S.
Department of Justice website.

These datasets are difficult to download using standard download
managers due to:

 short-lived authorization cookies  anti-automation challenges 
intermittent authorization expiration (401 errors)  large dataset size
 unstable long-running downloads  pagination behavior that repeats
pages

This tool uses a real browser session and human verification when
necessary, prioritizing reliability over aggressive scraping.

  -----------------------------------
  VERSION 2 CHANGES (MAJOR UPGRADE)
  -----------------------------------

This project has evolved from a single-dataset downloader into a full
crawler + downloader system.

Major upgrades include:

growth detection  Crash-safe downloads  Persistent dataset index 
Resume-safe operation  Automatic repair of missing files  Dataset
selection by user  Improved logging and recovery behavior

No hardcoded page limits remain.

  ---------------
  CORE FEATURES
  ---------------

Dataset Selection

Users can choose which datasets to download:

    1,3,5
    1-11
    9-11

Dynamic Page Detection

The crawler scans pages until no new PDFs appear, automatically adapting
to DOJ pagination changes.

Persistent Scan Index

Each dataset maintains its own index file:

    dataX/index_dataX.json

The index records:

     discovered PDFs
     source page numbers
     download status
     timestamps
     retry counts

This allows:

     safe resume
     crash recovery
     missing file repair
     dataset updates detection

Crash-Safe Downloads

Downloads are written safely using a temporary file:

    filename.pdf.part

Only after download completes successfully is the file renamed to:

    filename.pdf

This prevents crashes from leaving corrupted files marked complete.

Automatic Repair of Missing Files

If PDFs are missing locally but listed in the index, they are
automatically downloaded on the next run.

No cleanup scripts are required anymore.

Human Verification Support

When DOJ presents a verification challenge:

     Browser pauses
     User completes verification
     Script resumes automatically

No authentication bypass is attempted.

Conservative Download Behavior

Requests are throttled to reduce lockouts and server stress.

No parallel download hammering is used.

Persistent Logging

All actions are recorded in:

    download.log

  ------------------
  OUTPUT STRUCTURE
  ------------------

Example structure:

    data9/
        EFTA00012345.pdf
        EFTA00012346.pdf
        index_data9.json

    resume_data9.txt
    download.log

Files:

    PDFs                Downloaded documents
    index_dataX.json   Dataset scan index
    resume_dataX.txt   Last scanned page
    download.log       Activity log

Do not move or rename files while the script is running.

  --------------
  REQUIREMENTS
  --------------

Python 3.9 or newer

Playwright with Chromium browser:

    pip install playwright
    playwright install chromium

  -------
  USAGE
  -------

Run from script directory:

    python epstein_ripper.py

You will be prompted for:

     dataset selection
     operating mode

  -------
  MODES
  -------

sync (recommended) Scan DOJ pages and download missing files.

scan Only update index, no downloads.

download Download missing files using existing index.

  --------------------
  FIRST RUN BEHAVIOR
  --------------------

1.  Browser window opens
2.  Complete verification if requested
3.  Wait until file list appears
4.  Press ENTER in terminal
5.  Script begins scanning and downloading

  -----------------
  RESUME BEHAVIOR
  -----------------

The script resumes automatically using:

     last scanned page
    persistent dataset index

Crashes and restarts are safe.

  -----------------
  IMPORTANT NOTES
  -----------------

Do NOT:

     close the browser window mid-run
     rename files during operation
     delete index files while running

Resume logic depends on them.

  ----------------
  KNOWN BEHAVIOR
  ----------------

DOJ pagination sometimes repeats pages.

The crawler stops scanning after several pages produce no new PDFs.

Some files may fail due to authorization expiration and will be retried
automatically on later runs.

  -----------------------
  LEGACY CLEANUP SCRIPT
  -----------------------

Older versions required a separate cleanup tool.

Version 2 automatically repairs missing downloads, making the cleanup
script unnecessary.


🧰 Utilities
---------------
The following utilities were added to support dataset validation, repair, and analysis — particularly in response to upstream HTML verification pages being saved as .pdf files during large pulls.

These tools are optional but strongly recommended for anyone working with large DOJ datasets.

active_watcher.py
-----------------
Purpose

active_watcher.py is a live corruption detection and containment utility designed to run alongside the DOJ dataset downloader.

It monitors the active dataset directory in real time and automatically detects when a downloaded file is not a valid PDF (e.g., HTML age-verification pages or server error pages).

use:
----
(start epstein-ripper and in another terminal window(tmux tile/duplicated window/etc) launch the active_watcher and give it the directory to monitor
-----

When corruption is detected, it:

Emits a visible multi-line error alert
Triggers a terminal bell
Moves the corrupted file into a quarantine/ folder
Pauses execution
Waits for user confirmation before resuming

It maintains a persistent state file so it can resume safely after interruption.

corruption_scan.py
-------------
Purpose

corruption_scanner.py is a manual cleanup utility that scans an existing dataset directory for corrupted files and isolates them.

Unlike active_watcher.py, this tool does not run continuously.
It performs a one-time sweep of a directory when executed.

What It Does
--------------
Prompts for a directory to scan
Scans all files in the root of that directory (non-recursive)
Reads the file header (first ~8KB)
Verifies valid PDF signature (%PDF-)

Detects HTML corruption markers such as:

<html>
<!doctype html>
<head>
<title>

Moves corrupted files into:
<dataset_directory>/quarantine/

Auto-renames files on collision (e.g., file__q1.pdf)

Prints a final summary:
Total files scanned
Corrupted files moved
Clean files remaining

Why This Exists:
---------------
If a download session was interrupted, rate-limited, or corrupted before active_watcher.py was running, HTML documents may already exist inside the dataset directory.

This utility allows you to:
---------------
Clean up after an overnight run
Sweep an older dataset revision
Prepare a directory for a clean re-download attempt
Verify dataset integrity before indexing or searching

Use Case
---------------
Run corruption_scanner.py when:
The downloader is stopped
You suspect prior corruption occurred
You want a fast integrity check
You are preparing for a repair pass

It is safe to run multiple times.
Already quarantined files will not be rescanned.
-------------------------------
# END DATA VERIFICATION UTILS #
-------------------------------
# IMAGE EXTRACTION UTILITY    #
# image_ripper.py             #
-------------------------------
image_ripper.py — Bulk PDF Image Extractor

image_ripper.py is a GUI utility for extracting embedded images from large collections of PDFs. It was built for high-volume dataset analysis (e.g., DOJ disclosures) and supports incremental re-runs without duplicating work.

What It Does
------------
Recursively scans one or more selected folders for .pdf files
Extracts all embedded image objects using PyMuPDF

Saves extracted images into:
./ripped_images/

Generates a mapping file:
ripped_images/image_map.txt

Each entry correlates:
<extracted_image_filename> → <source_pdf_path> + page number + image index
------------
⚡Incremental Processing (Smart Re-Runs)
------------
The utility tracks processed PDFs using:
ripped_images/processed_pdfs.txt

Each PDF is recorded with:
-------------
absolute_path | file_size | last_modified_time

On subsequent runs:
-------------
 Unchanged PDFs are skipped instantly
 Newly added PDFs are processed
 Modified PDFs are automatically reprocessed
 No duplicate re-ripping of previously processed files

This allows safe re-running on growing datasets without wasting time or disk space.

What Gets Extracted?
-------------
Embedded image objects (photos, scans, screenshots, etc.)
Full-page scans in rasterized PDFs
Logos, stamps, and embedded graphics

Note: This utility does not render vector text pages to images. It only extracts actual embedded image objects.
-------------
📦 Output Structure
-------------
project_directory/
├── image_ripper.py
├── ripped_images/
│   ├── image_map.txt
│   ├── processed_pdfs.txt
│   ├── <extracted images...>

Requirements:
--------------
pip install pymupdf pillow


Designed for:
Large public disclosure datasets
Forensic document analysis
Quickly browsing extracted visuals independent of original PDFs
Tracking image origins via mapping log

[ END UTILS ]


  ------------
  DISCLAIMER
  ------------

This tool accesses publicly available DOJ materials.

It does not bypass authentication or security controls.

All verification steps require explicit human interaction.

Provided for archival, research, and transparency purposes.

Use responsibly and in accordance with applicable laws and site terms.
































































