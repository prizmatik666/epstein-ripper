#!/usr/bin/env python3
"""
just used to get a total file count in an index file

index_stats.py (Epstein index-aware)

Counts *one entry per PDF filename* (e.g., EFTA02232977.pdf), NOT every .pdf mention.

Input can be:
- a JSON index file
- a SQLite .db/.sqlite file
- a directory (it will scan for .json and .db/.sqlite/.sqlite3)

"""

import json
import re
import sqlite3
from pathlib import Path
from datetime import datetime
from urllib.parse import urlparse

JSON_EXTS = {".json"}
DB_EXTS = {".db", ".sqlite", ".sqlite3"}

# Prefer Epstein-style canonical IDs first; fallback to generic *.pdf
EFTA_RE = re.compile(r"\b(EFTA\d{6,}\.pdf)\b", re.IGNORECASE)
GEN_PDF_RE = re.compile(r"\b([A-Za-z0-9._-]+\.pdf)\b", re.IGNORECASE)

# ----------------- utils -----------------

def clean_input_path(s: str) -> Path:
    s = s.strip().strip('"').strip("'")
    if not s:
        s = "."
    return Path(s).expanduser().resolve()

def human_bytes(n: int) -> str:
    units = ["B", "KB", "MB", "GB", "TB"]
    f = float(n)
    for u in units:
        if f < 1024.0 or u == units[-1]:
            return f"{f:.2f} {u}" if u != "B" else f"{int(f)} B"
        f /= 1024.0
    return f"{n} B"

def safe_size(p: Path) -> int:
    try:
        return p.stat().st_size
    except Exception:
        return 0

def basename_from_url_or_path(s: str) -> str:
    """
    Normalize string to a basename-ish form (drop query/fragment if URL).
    """
    s = s.strip()
    try:
        u = urlparse(s)
        if u.scheme and u.netloc:
            path = u.path or ""
            base = path.split("/")[-1]
            return base
    except Exception:
        pass
    # Not a URL; just take last path chunk
    return s.split("/")[-1].split("\\")[-1]

def extract_canonical_pdf_name(s: str) -> str | None:
    """
    Extract canonical PDF basename for counting.
    - Prefer EFTA########.pdf pattern.
    - Fallback to any *.pdf basename.
    Returns normalized lowercase filename or None.
    """
    if not isinstance(s, str):
        return None
    base = basename_from_url_or_path(s)

    m = EFTA_RE.search(base)
    if m:
        return m.group(1).lower()

    m2 = GEN_PDF_RE.search(base)
    if m2:
        return m2.group(1).lower()

    return None

# ----------------- JSON analysis -----------------

def json_walk(obj, out_set: set[str]):
    if obj is None:
        return
    if isinstance(obj, str):
        name = extract_canonical_pdf_name(obj)
        if name:
            out_set.add(name)
        return
    if isinstance(obj, list):
        for x in obj:
            json_walk(x, out_set)
        return
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(k, str):
                name = extract_canonical_pdf_name(k)
                if name:
                    out_set.add(name)
            json_walk(v, out_set)

def analyze_json_index(p: Path):
    try:
        data = json.loads(p.read_text(encoding="utf-8", errors="replace"))
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}", "unique": 0, "method": "error", "samples": []}

    # Method 1: top-level dict keyed by pdf filenames (your described structure)
    if isinstance(data, dict):
        keys = list(data.keys())
        key_hits = []
        for k in keys:
            if isinstance(k, str):
                nm = extract_canonical_pdf_name(k)
                if nm and nm.endswith(".pdf"):
                    key_hits.append(nm)
        # If a meaningful portion of keys are PDFs, treat as authoritative
        if len(key_hits) >= 5 and len(key_hits) >= max(10, int(0.2 * max(1, len(keys)))):
            uniq = sorted(set(key_hits))
            return {
                "ok": True,
                "error": "",
                "unique": len(uniq),
                "method": "top_level_keys",
                "samples": uniq[:10],
            }

    # Method 2: fallback — scan values, but count UNIQUE canonical basenames
    uniq_set: set[str] = set()
    json_walk(data, uniq_set)
    uniq = sorted(uniq_set)
    return {
        "ok": True,
        "error": "",
        "unique": len(uniq),
        "method": "recursive_extract_unique_basenames",
        "samples": uniq[:10],
    }

# ----------------- SQLite analysis -----------------

def sqlite_tables(con):
    cur = con.cursor()
    cur.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type='table' AND name NOT LIKE 'sqlite_%';"
    )
    return [r[0] for r in cur.fetchall()]

def sqlite_text_columns(con, table_name: str):
    cols = []
    cur = con.cursor()
    try:
        cur.execute(f'PRAGMA table_info("{table_name}")')
        rows = cur.fetchall()
        for _cid, name, ctype, *_rest in rows:
            ctype_str = (ctype or "").upper()
            if ("CHAR" in ctype_str) or ("CLOB" in ctype_str) or ("TEXT" in ctype_str) or (ctype_str == ""):
                cols.append(name)
    except Exception:
        pass
    return cols

def analyze_sqlite_index(p: Path, timeout_s: float = 2.0, max_tables: int = 200):
    try:
        uri = f"file:{p.as_posix()}?mode=ro"
        con = sqlite3.connect(uri, uri=True, timeout=timeout_s)
        cur = con.cursor()
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}", "unique": 0, "method": "error", "samples": []}

    # Provide REGEXP + extraction helper inside SQLite
    def regexp(pattern, value):
        if value is None:
            return 0
        try:
            return 1 if re.search(pattern, str(value), re.IGNORECASE) else 0
        except Exception:
            return 0

    con.create_function("REGEXP", 2, regexp)

    uniq = set()
    samples = set()

    # We will extract EFTA...pdf when possible; else *.pdf
    # SQLite doesn't have regex-extract built-in, so we pull distinct candidates and extract in Python (capped).
    try:
        tables = sqlite_tables(con)[:max_tables]
        for t in tables:
            cols = sqlite_text_columns(con, t)
            if not cols:
                continue

            for c in cols:
                # Quick filter: any rows containing 'pdf'?
                try:
                    cur.execute(f'SELECT COUNT(*) FROM "{t}" WHERE LOWER("{c}") LIKE "%pdf%";')
                    n = int(cur.fetchone()[0])
                except Exception:
                    continue
                if n == 0:
                    continue

                # Pull distinct candidates (cap) and extract canonical basenames in Python
                try:
                    cur.execute(
                        f'SELECT DISTINCT "{c}" FROM "{t}" '
                        f'WHERE LOWER("{c}") LIKE "%pdf%" LIMIT 5000;'
                    )
                    vals = [r[0] for r in cur.fetchall() if isinstance(r[0], str)]
                except Exception:
                    continue

                for v in vals:
                    nm = extract_canonical_pdf_name(v)
                    if nm:
                        uniq.add(nm)
                        if len(samples) < 10:
                            samples.add(nm)

        con.close()
        return {
            "ok": True,
            "error": "",
            "unique": len(uniq),
            "method": "distinct_strings_then_extract_unique_basenames",
            "samples": sorted(samples)[:10],
        }
    except Exception as e:
        try:
            con.close()
        except Exception:
            pass
        return {"ok": False, "error": f"{type(e).__name__}: {e}", "unique": 0, "method": "error", "samples": []}

# ----------------- file gathering -----------------

def gather_targets(target: Path):
    json_files = []
    db_files = []

    if target.is_file():
        ext = target.suffix.lower()
        if ext in JSON_EXTS:
            json_files.append(target)
        elif ext in DB_EXTS:
            db_files.append(target)
        return json_files, db_files

    for p in target.rglob("*"):
        if not p.is_file():
            continue
        ext = p.suffix.lower()
        if ext in JSON_EXTS:
            json_files.append(p)
        elif ext in DB_EXTS:
            db_files.append(p)

    return json_files, db_files

# ----------------- main -----------------

def main():
    print("Enter folder OR index file path (.json / .db/.sqlite/.sqlite3)")
    print("Use '.' for current directory\n")
    raw = input("Path: ").strip()
    target = clean_input_path(raw)

    if not target.exists():
        print(f"Invalid path: {target}")
        raise SystemExit(1)

    json_files, db_files = gather_targets(target)

    print("\n" + "=" * 70)
    print("EPSTEIN INDEX STATS (COUNT ONE ENTRY PER PDF FILENAME)")
    print("=" * 70)
    print(f"Target: {target}")
    print(f"Time:   {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print()

    if not json_files and not db_files:
        print("No .json or .db/.sqlite index files found.")
        print("=" * 70 + "\n")
        return

    # JSON
    if json_files:
        print("-" * 70)
        print("JSON INDEX FILES")
        print("-" * 70)
        for p in sorted(json_files):
            res = analyze_json_index(p)
            sz = human_bytes(safe_size(p))
            name = p.name if not target.is_dir() else str(p.relative_to(target))

            if not res["ok"]:
                print(f"[JSON] {name} | size={sz} | ERROR: {res['error']}")
                continue

            print(f"[JSON] {name} | size={sz} | PDF_ENTRIES={res['unique']} | method={res['method']}")
            if res["samples"]:
                print("  samples:", ", ".join(res["samples"][:5]))
        print()

    # SQLite
    if db_files:
        ans = input("Inspect SQLite index DB(s) too? (y/N): ").strip().lower()
        if ans == "y":
            print("\n" + "-" * 70)
            print("SQLITE INDEX FILES")
            print("-" * 70)
            for p in sorted(db_files):
                res = analyze_sqlite_index(p)
                sz = human_bytes(safe_size(p))
                name = p.name if not target.is_dir() else str(p.relative_to(target))

                if not res["ok"]:
                    print(f"[DB]  {name} | size={sz} | ERROR: {res['error']}")
                    continue

                print(f"[DB]  {name} | size={sz} | PDF_ENTRIES={res['unique']} | method={res['method']}")
                if res["samples"]:
                    print("  samples:", ", ".join(res["samples"][:5]))
            print()
        else:
            print("Skipping SQLite inspection.\n")

    print("=" * 70 + "\n")


if __name__ == "__main__":
    main()