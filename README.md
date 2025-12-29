# epstein-ripper
downloads .pdf files from DOJ website / epstein data-sets 

DOJ Dataset-8 PDF Downloader (Resumable)

A resumable, human-verified Playwright downloader for the U.S. Department of Justice Epstein Data Set 8 document release.

This tool is designed to reliably download a large, gated PDF dataset that cannot be fetched with normal download managers due to:

short-lived authorization cookies

anti-automation challenges

non-resumable HTTP downloads

intermittent 401 errors mid-run

The script is intentionally conservative, transparent, and human-in-the-loop where required.

FEATURES

Resumable downloads
Automatically resumes from the last successful page and file
Survives crashes, restarts, and auth expiration

Human-verified access
Uses Playwright with a real browser
Pauses when DOJ requires “I am not a robot” verification

Safe re-authentication
Detects expired authorization (401)
Creates a fresh browser context when needed

Throttled & polite
Built-in delays to reduce lockouts
Sequential downloads only (no parallel hammering)

Persistent logging
Full activity log (download.log)
Resume state stored in resume_state.txt

OUTPUT STRUCTURE

epstein_dataset_8_pdfs/ downloaded PDFs
download.log detailed run log
resume_state.txt last known good page

Do NOT rename or move files until the full run completes.

REQUIREMENTS

Python 3.9 or newer

Playwright (Chromium)

Install dependencies:

pip install playwright
playwright install chromium

USAGE

Run the script from its working directory:

python3 downloader.py

FIRST RUN

A Chromium browser window will open

If prompted, click “I am not a robot”

Wait until the file list is visible

Press ENTER in the terminal to continue

SUBSEQUENT RUNS

The script resumes automatically

No page re-scanning unless required

Re-auth only happens when DOJ expires the session

RESUME LOGIC (HOW IT WORKS)

The downloader tracks progress using two independent signals:

Downloaded filenames
Determines the last successfully saved PDF

resume_state.txt
Stores the last page known to contain downloaded files

On restart:

If a resume state exists, start directly from that page

If not, perform a one-time scan to locate the correct page

After that, resumes are instant

This avoids guessing page numbers or relying on filename math.

IMPORTANT NOTES

Do NOT close the browser window manually during a run
The script controls browser lifecycle

Do NOT rename or delete PDFs mid-run
Resume logic relies on filenames

This tool does NOT bypass authentication
It pauses and waits for explicit user verification when required

KNOWN BEHAVIOR
--------------
A small number of files may fail during the main run due to auth expiry

This is expected and handled via a follow-up “missing files” pass
-the cleanup program makes a list of all the files on doj website to
create a master file list- then compares that against what is in the local
folder. It makes a list of needed files and downloads them.

The repository intentionally separates bulk acquisition from gap repair

DISCLAIMER

This tool is provided for research, archival, and transparency purposes.

It accesses publicly available DOJ materials and does not attempt to circumvent security controls.
All authentication steps require explicit human interaction.

Use responsibly and in accordance with applicable laws and terms.
