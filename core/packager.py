"""
JobHunt AI Studio - Packager
Bundles verified resume PDFs, neutralized summary CSV, and HTML reports into a ZIP archive.
Ensures packaging failures never corrupt or discard completed PDFs.
Supports independent packaging retries.
"""

from __future__ import annotations

import io
import logging
import os
import zipfile
from pathlib import Path
from typing import List, Optional, Union

logger = logging.getLogger("JobHuntAIStudio")


def build_batch_zip_bytes(
    pdf_paths: List[str],
    summary_csv_str: Optional[str] = None,
    summary_html_str: Optional[str] = None,
) -> bytes:
    """
    Creates an in-memory ZIP archive bytes buffer containing:
    - Verified single-page resume PDFs in 'resumes/'
    - batch_applied_summary.csv
    - batch_report.html
    Safely ignores nonexistent or 0-byte files so packaging succeeds cleanly.
    """
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        # 1. Add verified PDF resumes
        added_names = set()
        for p in pdf_paths:
            if not p:
                continue
            path_obj = Path(p)
            if path_obj.is_file() and path_obj.stat().st_size > 0:
                filename = path_obj.name
                if filename in added_names:
                    # Prevent zip collision
                    filename = f"{path_obj.stem}_{len(added_names)}{path_obj.suffix}"
                added_names.add(filename)
                arcname = f"resumes/{filename}"
                zf.write(path_obj, arcname=arcname)
            else:
                logger.warning(f"Packager skipping invalid or missing PDF: {p}")

        # 2. Add Summary CSV
        if summary_csv_str:
            zf.writestr("batch_applied_summary.csv", summary_csv_str.encode("utf-8"))

        # 3. Add Summary HTML
        if summary_html_str:
            zf.writestr("batch_report.html", summary_html_str.encode("utf-8"))

    buf.seek(0)
    return buf.getvalue()


def save_batch_zip(
    output_path: Union[str, Path],
    pdf_paths: List[str],
    summary_csv_str: Optional[str] = None,
    summary_html_str: Optional[str] = None,
) -> str:
    """Writes the batch ZIP file to disk atomically."""
    out_file = Path(output_path)
    out_file.parent.mkdir(parents=True, exist_ok=True)
    zip_bytes = build_batch_zip_bytes(pdf_paths, summary_csv_str, summary_html_str)
    
    # Write to temp file first, then atomic rename
    tmp_file = out_file.with_suffix(".tmp")
    with open(tmp_file, "wb") as f:
        f.write(zip_bytes)
    tmp_file.replace(out_file)
    return str(out_file)
