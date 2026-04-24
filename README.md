# EPSTEIN-RIPPER
Tools for automated indexing and download of DOJ epstein files data-sets.

NEW-UPGRADE :: 4/24/2026
- faster
    - upgraded to sqlite index use
    - massive speed increase over json
      when downloading / working with large index's
- stronger
    - error handling is pretty bullet  proof now, imo. Things are working very well. Long hands-free download sessions.

- smarter
    - upgraded the 'scan' mode selection for the main(EpRip) program. 
    - Combined the functionality of previous index_tools/ into one tool
    db_scanner . Which is also a scan/indexer , seperate from EpRip that performs the same battle-tested scanning. It can be used to get stats from your index files, repair them (match on-disk vs downloaded= value in index files). And other index management features.

- prettier
    - color coded EpRip ux. Over long downloads/scans with lines streaming, everything blends together after a while. I color coded features of the user interface to make it faster to visually acquire relevant information from the readout.

*IMPORTANT*
- use index_convertor.py to quickly convert your current JSON index's over to sqlite.
- db_scanner can do this also with it's import json mode, but it's not as straight forward as the index_convertor
- using sqlite over json will really open up the speed and advantage of this new upgrade.

## Overview

The DOJ dataset pages are not especially crawler-friendly. In practice, the project has had to deal with:
- pagination that repeats or remixes results
- no trustworthy last-page indicator
- short-lived auth/session state
- abuse-deterrent gates
- occasional non-PDF payloads served from PDF-looking endpoints

The goal of this repo is straightforward:
- build reliable indexes of the DOJ file listings
- download PDFs safely and resumably
- repair index/download-state drift when disk and index no longer agree
- provide utilities for validation, conversion, and post-processing

## Recommended Workflow

For most users:
1. use `NEW_EpRip.py` for normal scanning/downloading work
2. use `db_scanner_NEW.py` when you want deeper control over index building, resume/rewalk behavior, repair, or SQLite maintenance
3. keep your working index in SQLite
4. treat older JSON indexes as import sources, not as the preferred working format

If you have an older JSON index theres 2 ways to convert to sqlite:
[ method 2 is more straightforward imo ]
METHOD 1 - db_scanner.py

1. start `db_scanner_NEW.py` and pick your dataset
2. it will detect .sqlite and .json files. 
3. choose `Import JSON into SQLite`
4. it will prompt for the .json to use
5. point it at a new or existing SQLite target
6. continue all future work against the SQLite index

METHOD 2 - index_convertor.py

1. start 'index_convertor.py'
2. pick dataset #
3. pick mode JSON-> SQLite   (it also works both ways( SQLITE->JSON) 
4. it will show existing JSON in that dataset's dir. pick the one you want
   to use.
5. enter the output path/filename for the new .sqlite file that will be        created
6. thats it. i left this util in, even though the db_scanner can do the same   because its a more straightforward process. db_scanner contains most eve    ry previous index_files util from the previous release but this main con    version is paramount to experiencing the speed of the updated version of    EpRip. 
## Main Programs

### `NEW_EpRip.py`

Primary ripper/downloader.

Use this when you want the main integrated workflow:
- select one or more datasets
- scan DOJ pages
- download missing PDFs
- resume interrupted work
- keep index and download progress together in the main ripper flow

Core operating modes:
- `sync` = scan + download
- `scan` = update index only
- `download` = download missing files from an existing index

`NEW_EpRip.py` is the best default choice if your goal is to archive datasets end-to-end.

### `db_scanner_NEW.py`

Standalone index scanner and index maintenance utility.

Use this when you want tighter control over index operations than the main ripper exposes.

It can:
- scan DOJ dataset pages into an EpRip-compatible SQLite index
- resume discovery scans
- rewalk existing indexed ranges
- repair suspect/error pages
- import legacy JSON indexes into SQLite
- sync on-disk PDFs into the database
- repair download-state against actual disk state
- audit duplicate-like conditions
- produce disk/DB consistency reports
- duplicate an active DB into a clean shareable copy with download-state reset

This tool now contains the practical functionality that used to live in older standalone index repair/maintenance utilities.

If you prefer to separate indexing from downloading, or just prefer the db_scanner tool's layout, `db_scanner_NEW.py` can be used as the standalone scanner/index builder instead of relying on the main ripper for dataset scanning- but EpRip does do everything self contained (scan+download, scan, download) 

## Index Format

Recommended format:
- SQLite

Why:
- faster for large indexes
- safer for ongoing maintenance
- supports richer scan state and repair workflows
- matches the current EpRip-compatible utility path

Legacy format still supported:
- JSON import into SQLite
* json still supported but conversion to sqlite is HIGHLY recommended *
* the gains in download speed for SQLite is MASSIVE in comparisson! *
Current JSON import policy is preservation-first:
- existing SQLite rows are preserved
- JSON entries are normalized into the current schema
- only missing filenames are inserted

## Repo Layout

Intended top-level structure:

```text
index_files/
corruption_scan.py
db_scan_readme.md
db_scanner_NEW.py
filter_ripped_images.py
image_ripper.py
index_convertor.py
LEGACY_auto_ep_rip.py
NEW_EpRip.py
README.md
requirements.txt
```

### `index_files/`
index_files/ contains some premade index files for datasets 9-12
The index files are in zip files since they're over 100mb's. in json format.
will need to be converted over to sqlite . 

to use, place an index file into a corresponding data#/ directory that lives in the epstein-ripper root dir.

this is the dir structure EpRip excpects, However this new version lets you choose from discovered index files, as long as they're contained in the corresponding data# dir , where # == 1-12.

epstein-ripper
-data#/
--index_data#.sqlite

### `index_convertor.py`

Format conversion utility for index files.

Useful when someone specifically needs to flip supported EpRip index formats 

json -> sqlite
sqlite -> json

db_scanner can move json to sqlite also,
but this standalone util is a solid straight forward way to convert EpRip compatable index files.

the recommended long-term format For EpRip is now SQLite.

### `LEGACY_auto_ep_rip.py`
Older ripper version kept for reference/compatibility.

i will leave this in the repo for a period of time before removing. Not sure how long to wait. maybe a month or so from now i'll consider removing it.

The current preferred ripper is `NEW_EpRip.py`.

### `corruption_scan.py`
LEGACY UTIL 
not really needed anymore, i kept it in for a just-in-case/nostalgia purpose
One-time corruption sweep utility.

Use it to:
- scan a dataset directory
- validate PDF signatures
- quarantine obvious bad/corrupt files

After removing bad files from disk, use `db_scanner_NEW.py` utilities to reconcile download-state in the index.

### `image_ripper.py`

Embedded image extraction utility for  PDF collections.

Bulk embedded image extractor (GUI).

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

### `filter_ripped_images.py`

-sorts a dir containing images(recursive)
- And moves all black(redatcted)
  images
- images that appear to be all text/documents
- other images that have traits that dont seem like an
  image/picture.
  
- It puts these in different categories/buckets.
  moving them to new categorized folders for review.
- Using copy mode can balloon hard disk data and cause problems
  when used on a large ammount of data!
  - recommended to use Move mode. 

example:
I ran image_ripper on the datasets i have (far from complete, but several hundred thousand pdf's) - i ended up with over 400k+ image files ripped. After running this filter program on my ripped_images, i reviewed what it pulled out and it was very accurate. Not many images that I needed to save out of the images it pulled out. I havent gone through all of what was left behind, but i did go through the pulls - and out of the almost half million images, over 300k were moved from the scanned dir. to the sorted dirs

making, imo, this a very valuable tool in the pre-cleaning stage of going through the extracted images from using image_ripper.py.

because of the way the pdf's were generated alot of them themselves register as 'image' and get saved as image files- along with the extracted content. This problem makes this sorting tool a neccessary step as a Post-processing helper for extracted images.

## Data Safety / Integrity

This project is built around preserving operator confidence:
- downloads use validation-aware handling
- index work is resumable
- SQLite is the preferred working format
- JSON import is additive and non-destructive to existing SQLite rows
- disk repair utilities reconcile stale download-state
- `Ctrl+C` shutdown in the scanner reports saved progress and resume position

The duplicate-DB utility in `db_scanner_NEW.py` is specifically intended for:
- creating a clean shareable index
- preserving a working DB while producing a reset copy
- letting another operator re-repair download-state against their own local disk later

## Choosing Between The Two Main Tools

Use `NEW_EpRip.py` when:
- you want the main archival workflow
- you want one tool to handle scanning and downloading together ( or seperate )
- you want the default operator experience
- the workflow is straight forward and does everything needed.


db_scanner has specialized functionality for index's and scanning
Use `db_scanner_NEW.py` when:
- you want standalone index scanning
- you want to migrate legacy JSON indexes into SQLite
- you want index repair or disk reconciliation
- you want a clean copy of an index
- you want more direct control over index behavior and maintenance

## Requirements

Baseline requirements:
- Python 3.9+
- Playwright
- Chromium via Playwright

Quick Start

``` bash
git clone https://github.com/prizmatik666/epstein-ripper
cd epstein-ripper
pip install -r requirements.txt
playwright install chromium
python epstein_ripper.py
```

Some optional utilities may require additional packages depending on the tool being used.

## Practical Notes

- Do not rename or delete files while a scan/download job is actively running.
- Prefer SQLite for active work.
- Prefer importing old JSON indexes into SQLite rather than continuing to maintain JSON as the primary index format.
- Use the DB scanner utility section when disk contents and index state need to be reconciled.

## Support

If you are auditing, preserving, or extending the archive, the most useful thing you can do is use the newer SQLite-based workflow and keep the index state clean and reproducible.

The pursuit of truth, justice, and .pdf punishment is imperative. We're all a tool for change.
            - Prizm 

I had info for $upporting this project but someone tried to hack my paypal so I've removed the support info for that. Thanks =\