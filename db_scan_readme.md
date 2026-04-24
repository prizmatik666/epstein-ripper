# DB Scanner README

`db_scanner_upgrade.py` is a SQLite-backed DOJ Epstein dataset scanner and index utility.

It is designed to:
- scan DOJ dataset listing pages for EFTA PDF links
- store discovered files in an EpRip-compatible SQLite index
- support resume, rewalk, suspect-page repair, legacy JSON import, and index utility work
- optionally sync PDFs already present on disk into the working database

Recommended index format:
- use SQLite as the primary working index format
- treat legacy JSON indexes as import sources, not as the preferred working format

The recommended modern path is:
1. import or merge any legacy JSON index into SQLite
2. use the SQLite index for scanning, repair, and utility work going forward

SQLite is the recommended format because it is faster, safer for ongoing maintenance, and matches the current EpRip-compatible workflow.

## What It Uses

The scanner works against a selected SQLite database inside the chosen `data#/` directory.

Possible startup sources:
- existing SQLite index
- JSON index import into SQLite
- brand new SQLite index

Supported index file discovery in the dataset directory:
- `.sqlite`
- `.sqlite3`
- `.db`
- `.json`

File names do not need to follow `index_data#...` naming anymore.

## Startup Flow

Typical startup flow:
1. Pick dataset.
2. Pick index source.
3. Choose whether to add on-disk PDFs to the database.
4. Enter scan mode or utility mode.

If `Add on-disk PDFs to this database?` is `yes`, the tool immediately performs a startup disk sync and prints a summary before showing the scan menu.

## JSON Import Behavior

`Import JSON into SQLite` is now preservation-first.

It does **not** replace or delete the selected SQLite database.

Current import policy:
- normalize legacy JSON entries into the current EpRip-compatible SQLite row format
- compare JSON filenames against SQLite `files.filename`
- insert only filenames that are not already present
- skip filenames that already exist in SQLite
- import one selected JSON source per run

This means JSON import is:
- non-destructive to existing SQLite file rows
- additive only
- safe for merging older JSON indexes into a newer SQLite index

Recommended use:
- if you have an older `index_data#.json`, use this import mode to move it into the newer SQLite workflow
- after import, use the SQLite index as the main working index
- do not treat JSON as the preferred long-term working format anymore

It is not read-only, because it may:
- create the SQLite database if it does not already exist
- create required schema tables
- add missing rows
- write import metadata into `meta`

## On-Disk PDF Sync

When disk sync is enabled, the tool can:
- add valid EFTA PDFs from disk into the database
- mark matching DB rows as downloaded
- flip stale `downloaded=True` rows back to `False` if the file is missing or not a valid PDF
- update byte counts for matching files

Startup disk sync summary reports:
- PDFs found on disk
- valid EFTA PDFs
- new DB rows added
- existing rows updated
- marked downloaded
- flipped to false
- byte counts updated
- already downloaded
- missing on disk
- invalid PDFs skipped
- non-EFTA PDFs skipped

## Scan Modes

Main scan actions:
- `Resume DISCOVERY scan`
- `Full REWALK -> frontier, then continue DISCOVERY`
- `Repair suspect/error pages only`
- `Rewalk custom range, then continue DISCOVERY`

The no-new streak threshold is prompted when a scan action is selected.

It is not prompted for:
- `Index utility work`
- `Show DB stats`

## Index Utility Work

Current utility actions:
- repair DB from disk by reconciling DB download state with actual files on disk
- audit duplicate-like entries
- generate a disk/DB consistency report
- duplicate the active DB into a new file and reset download-state in the copy

The disk repair utility now covers the old standalone index-repair style behavior inside the scanner workflow.

It can:
- mark `downloaded=True` when a valid PDF exists on disk
- flip `downloaded=True` back to `False` when the file is missing or not a valid PDF
- clear stale download-state fields when a row is flipped back to false
- update byte counts from valid files on disk

This means the scanner utility section is now the preferred place to perform index repair for active SQLite indexes.

If you have a legacy JSON index:
- first import it into SQLite
- then use the scanner utilities to maintain and repair the SQLite index

Notes on duplicates:
- exact duplicate filenames are not possible in SQLite because `files.filename` is the primary key
- duplicate-like audits check for repeated URLs and repeated extracted file numbers

## DB Stats

`Show DB stats` reports:
- distinct PDFs in index
- PDF refs seen on pages
- EFTA refs seen on pages
- new PDFs discovered
- files downloaded
- files skipped
- pages scanned
- suspect/error pages
- frontier page
- resume state

## Ctrl+C / Safe Shutdown

`Ctrl+C` is handled with a shutdown summary instead of a raw traceback.

Interrupt summary includes:
- working DB path
- committed progress
- pages/files totals
- run-local new PDFs discovered
- last committed page
- last committed scan time
- resume page
- resume streak
- restart safety message

The scanner commits page/file progress during scanning, so restart after interruption is safe.

## Schema Notes

The SQLite index is written in an EpRip-compatible format:
- `meta`
- `files`

Scanner-only state is kept in:
- `pages`
- `resume_state`

Legacy JSON or legacy SQLite data is normalized into the current row shape so it continues to work with:
- EpRip-compatible readers
- this scanner
- the current utility/reporting features

## Practical Safety Notes

Safe operations:
- using an existing SQLite index
- starting a new SQLite index at a new path
- JSON import merge into SQLite
- utility audits
- duplicating the active DB into a reset clean copy

Operations that write to the DB:
- disk sync
- JSON import merge
- scanning
- repair utilities
- duplicate-and-reset utility writes only to the new copy, not the source DB

Current design goal:
- preserve existing SQLite file rows unless the selected action is explicitly meant to add or update data

## Recommended Workflows

### Upgrade Legacy JSON To The New Standard

Recommended path for older JSON indexes:
1. start `db_scanner_upgrade.py`
2. pick the dataset
3. choose `Import JSON into SQLite`
4. select a JSON source from the discovered JSON files in the dataset directory
5. enter a target SQLite path
   the target SQLite does not need to already exist; the tool will create it if needed
5. optionally enable on-disk PDF sync if you want current files on disk folded into the DB
6. use the resulting SQLite index as the main working index going forward

Why this is recommended:
- SQLite is the current faster working format
- the scanner and utilities are built around the SQLite workflow
- legacy JSON entries are normalized into the current EpRip-compatible schema during import
- future repair and maintenance is cleaner in SQLite than in the older JSON workflow

Recommended after import:
- run `Index utility work -> Repair DB from disk` if you want DB download-state reconciled against what is actually on disk
- use `Show DB stats` to confirm row counts and scan state
- continue future scans against the SQLite index, not the old JSON file

### Build A Clean Shareable Copy

If you want a copy of the index without your local download-state:
1. open the active SQLite index
2. go to `Index utility work`
3. choose `Duplicate active DB and reset download state`
4. save the duplicate under a new filename

This produces a clean shareable SQLite index while leaving the active source DB unchanged.

### Reconcile A Working SQLite Index With Disk

If files on disk and DB download-state may have drifted:
1. open the active SQLite index
2. go to `Index utility work`
3. choose `Repair DB from disk`

This will:
- mark valid PDFs on disk as downloaded
- flip stale downloaded rows back to false when files are missing or invalid
- refresh download-related fields such as byte counts
