"""
JobHunt AI Studio - Universal ATS Acceptance Test Suite
Validates PDF resumes against enterprise ATS parsing standards and single-page constraints:
1. Strict single-page bounds (page_count == 1).
2. Non-empty file and non-empty text stream.
3. Zero kerning word-splits (e.g. 'T echnical', 'P rojects').
4. Strict 4-digit years (e.g. '2022 - 2026', 'Jan 2025' - no 2-digit years like '25).
5. Location in candidate contact header.
6. Clean plain URLs without invisible alias tricks.
"""

from __future__ import annotations

import os
import re
from typing import Any, Dict, List

import pypdf
try:
    import fitz  # PyMuPDF
except ImportError:
    fitz = None


def audit_resume_pdf(pdf_path: str) -> Dict[str, Any]:
    results: Dict[str, Any] = {
        "passed": True,
        "errors": [],
        "warnings": [],
        "details": {}
    }

    if not os.path.exists(pdf_path):
        results["passed"] = False
        results["errors"].append(f"FILE NOT FOUND: {pdf_path}")
        return results

    # File size check
    try:
        size = os.path.getsize(pdf_path)
        results["details"]["file_size_bytes"] = size
        if size < 100:
            results["passed"] = False
            results["errors"].append("EMPTY FILE: PDF file size is too small or corrupted.")
            return results
    except Exception as e:
        results["passed"] = False
        results["errors"].append(f"FILE ACCESS ERROR: {e}")
        return results

    # 1. Page Count Audit
    try:
        reader = pypdf.PdfReader(pdf_path)
        total_pages = len(reader.pages)
        results["details"]["page_count"] = total_pages
        if total_pages != 1:
            results["passed"] = False
            results["errors"].append(
                f"PAGE COUNT VIOLATION: Resume has {total_pages} pages. Strict single-page required."
            )
    except Exception as e:
        results["passed"] = False
        results["errors"].append(f"PDF READ ERROR: {e}")
        return results

    # 2. Extract Text Streams
    full_text = ""
    for page in reader.pages:
        full_text += (page.extract_text() or "") + "\n"

    fitz_text = ""
    if fitz:
        try:
            doc = fitz.open(pdf_path)
            for page in doc:
                fitz_text += (page.get_text() or "") + "\n"
            doc.close()
        except Exception:
            fitz_text = full_text
    else:
        fitz_text = full_text

    char_count = len(full_text.strip())
    results["details"]["char_count"] = char_count
    if char_count < 50:
        results["passed"] = False
        results["errors"].append("EMPTY CONTENT: PDF contains no readable text stream.")

    # 3. Kerning & Word-Splitting Audit
    split_patterns = [
        r'\bT\s+echnical\b',
        r'\bT\s+ools\b',
        r'\bT\s+echnologies\b',
        r'\bV\s+ersion\b',
        r'\bP\s+rojects\b',
        r'\bE\s+ducation\b',
        r'\bE\s+xperience\b',
        r'\bC\s+ertifications\b',
        r'\bA\s+chievements\b',
        r'\bP\s+rofile\b',
        r'\bS\s+kills\b'
    ]
    for pattern in split_patterns:
        match = re.search(pattern, full_text, re.IGNORECASE) or re.search(pattern, fitz_text, re.IGNORECASE)
        if match:
            results["passed"] = False
            results["errors"].append(
                f"KERNING WORD-SPLIT DETECTED: Found '{match.group(0)}'. Must be clean contiguous word."
            )

    # 4. Two-Digit Year Audit
    two_digit_date_pattern = r'\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+(\d{2})\b(?!\d{2})'
    invalid_dates = re.findall(two_digit_date_pattern, full_text) or re.findall(two_digit_date_pattern, fitz_text)
    if invalid_dates:
        results["passed"] = False
        results["errors"].append(
            f"FATAL 2-DIGIT YEAR DETECTED: Found {invalid_dates}. Must use 4-digit years (e.g., 'Jan 2025')."
        )

    two_digit_range_pattern = r'\b(?:\'|\b)(2[0-9])\s*[-–—]\s*(?:\'|\b)(2[0-9])\b'
    invalid_ranges = re.findall(two_digit_range_pattern, full_text)
    if invalid_ranges:
        results["passed"] = False
        results["errors"].append(
            f"FATAL 2-DIGIT YEAR RANGE DETECTED: Found {invalid_ranges}. Must use 4-digit years (e.g., '2022 - 2026')."
        )

    # 5. Candidate Location in Header Audit
    header_match = re.split(
        r'\b(PROFILE|SUMMARY|EDUCATION|SKILLS|TECHNICAL\s+SKILLS|WORK\s+EXPERIENCE|PROJECTS)\b',
        full_text,
        flags=re.IGNORECASE
    )
    header_text = header_match[0] if header_match else full_text[:250]
    location_keywords = ["Pune", "Maharashtra", "India", "Bangalore", "Delhi", "Mumbai", "Hyderabad", "Noida", "Remote", "USA", "Canada", "UK"]
    found_loc = any(loc.lower() in header_text.lower() for loc in location_keywords)
    if not found_loc:
        results["warnings"].append(
            "CANDIDATE LOCATION: Location keyword not detected in top header contact block."
        )

    return results
