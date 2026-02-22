import re
import threading
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext

# Primary extractor
try:
    import fitz  # PyMuPDF
except ImportError:
    fitz = None


def safe_name(s: str, max_len: int = 120) -> str:
    """Sanitize a string for filenames."""
    s = re.sub(r"[^\w\-.]+", "_", (s or "").strip())
    return s[:max_len] if len(s) > max_len else s


def find_pdfs_in_dirs(dirs: list[Path]) -> list[Path]:
    pdfs: list[Path] = []
    for d in dirs:
        if not d.exists():
            continue
        for p in d.rglob("*.pdf"):
            pdfs.append(p)
    return sorted(set(pdfs))


def ensure_output_dir(base_dir: Path) -> Path:
    out_dir = base_dir / "ripped_images"
    out_dir.mkdir(parents=True, exist_ok=True)
    return out_dir


def rip_images_from_pdf_pymupdf(pdf_path: Path, out_dir: Path, map_fp):
    """
    Extract embedded images using PyMuPDF.
    Writes extracted images into out_dir and logs mapping lines to map_fp.
    """
    doc = fitz.open(pdf_path)
    pdf_stem = safe_name(pdf_path.stem)

    extracted = 0
    for page_index in range(len(doc)):
        page = doc[page_index]
        image_list = page.get_images(full=True)

        for img_idx, img in enumerate(image_list, start=1):
            xref = img[0]
            try:
                base = doc.extract_image(xref)
            except Exception:
                continue

            img_bytes = base.get("image", None)
            img_ext = base.get("ext", "bin")

            if not img_bytes:
                continue

            filename = f"{pdf_stem}_p{page_index+1:04d}_img{img_idx:04d}.{img_ext}"
            out_path = out_dir / filename

            # Collision-safe (auto-increment)
            if out_path.exists():
                n = 2
                while True:
                    alt = out_dir / f"{pdf_stem}_p{page_index+1:04d}_img{img_idx:04d}_{n}.{img_ext}"
                    if not alt.exists():
                        out_path = alt
                        filename = alt.name
                        break
                    n += 1

            with open(out_path, "wb") as f:
                f.write(img_bytes)

            # Map line: image_file \t source_pdf \t page \t img_index \t xref
            map_fp.write(
                f"{filename}\t{pdf_path}\tpage={page_index+1}\timg={img_idx}\txref={xref}\n"
            )
            extracted += 1

    doc.close()
    return extracted


# ---------------- Incremental tracking (mtime+size) ----------------
def load_processed_records(record_path: Path):
    """
    Returns dict[path_str] = (size:int, mtime:float)
    """
    records = {}
    if not record_path.exists():
        return records

    with open(record_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or "|" not in line:
                continue

            # tolerate extra pipes by limiting split
            parts = line.split("|")
            if len(parts) < 3:
                continue

            path = parts[0]
            size = parts[1]
            mtime = parts[2]
            try:
                records[path] = (int(size), float(mtime))
            except ValueError:
                continue

    return records


def append_processed_record(record_path: Path, pdf_path: Path):
    stat = pdf_path.stat()
    with open(record_path, "a", encoding="utf-8") as f:
        f.write(f"{pdf_path}|{stat.st_size}|{stat.st_mtime}\n")


class ImageRipperGUI:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("PDF Image Ripper")
        self.root.geometry("900x520")

        self.input_dirs: list[Path] = []

        # Buttons row
        btn_frame = tk.Frame(root)
        btn_frame.pack(fill="x", padx=10, pady=10)

        tk.Button(btn_frame, text="Add Input FolderΓÇª", command=self.add_folder, width=18).pack(side="left")
        tk.Button(btn_frame, text="Clear Folders", command=self.clear_folders, width=14).pack(side="left", padx=(8, 0))
        tk.Button(btn_frame, text="Run Rip", command=self.run, width=12, height=2).pack(side="left", padx=(12, 0))
        tk.Button(btn_frame, text="Stop (soft)", command=self.request_stop, width=12).pack(side="left", padx=(8, 0))

        self.status_var = tk.StringVar(value="Add one or more folders, then Run.")
        tk.Label(root, textvariable=self.status_var, anchor="w").pack(fill="x", padx=10)

        # Folder list
        self.folder_list = tk.Listbox(root, height=6)
        self.folder_list.pack(fill="x", padx=10, pady=(6, 10))

        # Log output
        self.log = scrolledtext.ScrolledText(root, height=18)
        self.log.pack(fill="both", expand=True, padx=10, pady=(0, 10))

        self.stop_flag = False
        self.worker_thread = None

        if fitz is None:
            self.log_write("ERROR: PyMuPDF (fitz) not installed.\n")
            self.log_write("Install with: pip install pymupdf pillow\n")

    def log_write(self, msg: str):
        self.log.insert("end", msg)
        self.log.see("end")
        self.root.update_idletasks()

    def add_folder(self):
        path = filedialog.askdirectory(title="Choose a folder containing PDFs (recursive)")
        if not path:
            return
        p = Path(path)
        if p not in self.input_dirs:
            self.input_dirs.append(p)
            self.folder_list.insert("end", str(p))
            self.status_var.set(f"{len(self.input_dirs)} folder(s) queued.")
        else:
            self.status_var.set("Folder already added.")

    def clear_folders(self):
        self.input_dirs.clear()
        self.folder_list.delete(0, "end")
        self.status_var.set("Cleared. Add folders, then Run.")

    def request_stop(self):
        self.stop_flag = True
        self.status_var.set("Stop requestedΓÇª (will stop after current PDF finishes)")

    def run(self):
        if fitz is None:
            messagebox.showerror(
                "Missing dependency",
                "PyMuPDF (fitz) is not installed.\nRun: pip install pymupdf pillow",
            )
            return

        if not self.input_dirs:
            messagebox.showerror("No folders", "Add at least one input folder.")
            return

        if self.worker_thread and self.worker_thread.is_alive():
            messagebox.showwarning("Already running", "Ripping is already running.")
            return

        self.stop_flag = False
        self.worker_thread = threading.Thread(target=self.worker, daemon=True)
        self.worker_thread.start()

    # ---------------- PATCHED worker() (mtime incremental) ----------------
    def worker(self):
        base_dir = Path.cwd()
        out_dir = ensure_output_dir(base_dir)
        map_path = out_dir / "image_map.txt"
        record_path = out_dir / "processed_pdfs.txt"

        self.log_write(f"\nOutput folder: {out_dir}\n")
        self.log_write(f"Mapping file : {map_path}\n")
        self.log_write(f"Tracking file: {record_path}\n\n")

        pdfs = find_pdfs_in_dirs(self.input_dirs)
        if not pdfs:
            self.status_var.set("No PDFs found in selected folders.")
            self.log_write("No PDFs found.\n")
            return

        processed_records = load_processed_records(record_path)

        self.status_var.set(f"Found {len(pdfs)} PDFs. StartingΓÇª")
        self.log_write(f"Found {len(pdfs)} PDFs total.\n\n")

        total_imgs = 0
        processed = 0
        skipped = 0

        with open(map_path, "a", encoding="utf-8") as map_fp:
            map_fp.write("\n=== NEW RUN ===\n")
            map_fp.write(f"BaseDir: {base_dir}\n")
            map_fp.write("image_file\tsource_pdf\tpage\timg\txref\n")

            for i, pdf_path in enumerate(pdfs, start=1):
                if self.stop_flag:
                    self.status_var.set("Stopped.")
                    self.log_write("\nSTOPPED by user.\n")
                    break

                try:
                    stat = pdf_path.stat()
                except FileNotFoundError:
                    self.log_write(f"[{i}/{len(pdfs)}] MISSING (skipped): {pdf_path}\n")
                    continue

                key = str(pdf_path)
                current_meta = (stat.st_size, stat.st_mtime)

                if key in processed_records and processed_records[key] == current_meta:
                    skipped += 1
                    self.log_write(f"[{i}/{len(pdfs)}] SKIP (unchanged): {pdf_path.name}\n")
                    continue

                processed += 1
                self.status_var.set(f"Processing {i}/{len(pdfs)}: {pdf_path.name}")
                self.log_write(f"[{i}/{len(pdfs)}] {pdf_path}\n")

                try:
                    n = rip_images_from_pdf_pymupdf(pdf_path, out_dir, map_fp)
                    total_imgs += n
                    self.log_write(f"  -> extracted {n} image(s)\n")

                    # Record as processed (mtime+size)
                    append_processed_record(record_path, pdf_path)

                    # Keep our in-memory dict current too (prevents double work if same pdf appears twice)
                    processed_records[key] = current_meta

                except Exception as e:
                    self.log_write(f"  !! error: {e}\n")

                self.root.update_idletasks()

        self.status_var.set(f"Done. Processed: {processed}, Skipped: {skipped}, Images: {total_imgs}")
        self.log_write(
            f"\nDONE.\nProcessed: {processed}\nSkipped: {skipped}\nImages extracted: {total_imgs}\n"
        )


def main():
    root = tk.Tk()
    ImageRipperGUI(root)
    root.mainloop()


if __name__ == "__main__":
    main()
