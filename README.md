EPSTEIN-RIPPER

Reliable downloader and archival tool for DOJ Epstein dataset PDFs.

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

  ------------
  DISCLAIMER
  ------------

This tool accesses publicly available DOJ materials.

It does not bypass authentication or security controls.

All verification steps require explicit human interaction.

Provided for archival, research, and transparency purposes.

Use responsibly and in accordance with applicable laws and site terms.
































































