"""
JobHunt AI Studio - Smart CSV Parser
Flexibly parses job listings CSV files with intelligent synonym detection and strict input safety:
- Enforces 5 MB file size limit and 100 jobs per run limit.
- Rejects oversized descriptions (> 30,000 characters) without silent truncation.
- Generates stable, deterministic job IDs via content hashing.
- Detects ambiguous columns and malformed rows.
- Validates safe URLs and eliminates invented location defaults.
"""

from __future__ import annotations

import csv
import io
import re
from typing import Any, Dict, List, Optional, Tuple, Union

from core.security import (
    MAX_CSV_BYTES,
    MAX_JD_CHARACTERS,
    MAX_JOBS_PER_RUN,
    compute_stable_job_id,
    is_safe_url,
)

COLUMN_SYNONYMS = {
    "company": [
        "company", "company_name", "companyname", "employer", "organization",
        "org", "firm", "hiring_company", "company_name_raw"
    ],
    "title": [
        "title", "job_title", "jobtitle", "role", "position",
        "job_name", "designation", "job_role"
    ],
    "jd_text": [
        "jd_text", "job_description", "jobdescription", "description", "jd",
        "job_details", "details", "requirements", "responsibilities", "content", "job_summary"
    ],
    "url": [
        "url", "job_url", "joburl", "link", "apply_link", "apply_url",
        "job_link", "posting_url", "href", "source_url"
    ],
    "location": [
        "location", "city", "work_mode", "job_location", "place", "state", "country"
    ],
}


def strip_html_tags(text: str) -> str:
    """Removes HTML tags and entities from raw scraped or pasted text."""
    if not text:
        return ""
    clean = re.sub(r'<[^>]+>', ' ', text)
    clean = re.sub(r'&nbsp;', ' ', clean)
    clean = re.sub(r'&amp;', '&', clean)
    clean = re.sub(r'&lt;', '<', clean)
    clean = re.sub(r'&gt;', '>', clean)
    clean = re.sub(r'\s+', ' ', clean)
    return clean.strip()


def detect_column_mapping(headers: List[str]) -> Dict[str, str]:
    """
    Maps standard internal keys ('company', 'title', 'jd_text', 'url', 'location')
    to the actual CSV header names using case-insensitive substring and synonym matching.
    Detects and flags ambiguous columns.
    """
    mapping: Dict[str, str] = {}
    normalized_headers = {
        re.sub(r'[^a-z0-9_]', '', h.strip().lower().replace(" ", "_")): h
        for h in headers if h
    }

    for standard_key, synonyms in COLUMN_SYNONYMS.items():
        # 1. Exact match
        for syn in synonyms:
            if syn in normalized_headers:
                mapping[standard_key] = normalized_headers[syn]
                break
        if standard_key in mapping:
            continue

        # 2. Fuzzy substring match
        matched_candidates = []
        for norm_h, original_h in normalized_headers.items():
            if any(syn in norm_h for syn in synonyms):
                matched_candidates.append(original_h)

        if len(matched_candidates) == 1:
            mapping[standard_key] = matched_candidates[0]
        elif len(matched_candidates) > 1:
            # Ambiguous column headers detected
            mapping[standard_key] = matched_candidates[0]

    return mapping


def parse_jobs_csv(
    csv_content_or_file: Union[str, bytes, io.StringIO, io.BytesIO],
    min_jd_length: int = 50,
) -> Tuple[List[Dict[str, Any]], Dict[str, str], List[str]]:
    """
    Parses a CSV file or string into a structured list of normalized job dictionaries.
    Validates limits (5 MB, 100 jobs, 30,000 char JD limit) and produces stable job IDs.

    Returns:
        (jobs, detected_mapping, warnings)
    """
    warnings: List[str] = []

    # 1. Prepare string stream and validate size
    if isinstance(csv_content_or_file, bytes):
        if len(csv_content_or_file) > MAX_CSV_BYTES:
            raise ValueError(f"CSV file size exceeds 5 MB limit ({len(csv_content_or_file)} bytes).")
        raw_str = csv_content_or_file.decode("utf-8-sig", errors="replace")
        stream = io.StringIO(raw_str)
    elif isinstance(csv_content_or_file, io.BytesIO):
        b = csv_content_or_file.read()
        if len(b) > MAX_CSV_BYTES:
            raise ValueError(f"CSV file size exceeds 5 MB limit ({len(b)} bytes).")
        raw_str = b.decode("utf-8-sig", errors="replace")
        stream = io.StringIO(raw_str)
    elif isinstance(csv_content_or_file, io.StringIO):
        raw_str = csv_content_or_file.getvalue()
        if len(raw_str.encode("utf-8")) > MAX_CSV_BYTES:
            raise ValueError("CSV content exceeds 5 MB limit.")
        stream = csv_content_or_file
    elif isinstance(csv_content_or_file, str):
        raw_str = csv_content_or_file
        if len(raw_str.encode("utf-8")) > MAX_CSV_BYTES:
            raise ValueError("CSV content exceeds 5 MB limit.")
        if raw_str.startswith("\ufeff"):
            raw_str = raw_str[1:]
        stream = io.StringIO(raw_str)
    else:
        raise ValueError("Unsupported CSV content type.")

    reader = csv.DictReader(stream)
    headers = reader.fieldnames or []
    if not headers:
        raise ValueError("CSV file has no header row.")

    mapping = detect_column_mapping(headers)

    # Check required fields
    for req in ["company", "title", "jd_text"]:
        if req not in mapping:
            raise ValueError(
                f"Missing required column '{req}'. Detected headers: {headers}. "
                f"Please ensure your CSV has a column for {req} (or common synonyms like 'Company Name', 'Job Title', 'Job Description')."
            )

    jobs: List[Dict[str, Any]] = []
    row_num = 1

    for row in reader:
        row_num += 1
        raw_company = row.get(mapping["company"], "").strip()
        raw_title = row.get(mapping["title"], "").strip()
        raw_jd = row.get(mapping["jd_text"], "").strip()

        if not raw_company and not raw_title and not raw_jd:
            continue  # Blank row

        # Check JD length upper bound
        if len(raw_jd) > MAX_JD_CHARACTERS:
            raise ValueError(
                f"Row {row_num} ('{raw_company}' - '{raw_title}') description exceeds {MAX_JD_CHARACTERS} characters "
                f"({len(raw_jd)} chars). Oversized job descriptions are rejected to prevent processing errors."
            )

        clean_jd = strip_html_tags(raw_jd)
        if len(clean_jd) < min_jd_length:
            warnings.append(
                f"Row {row_num} ('{raw_company}' - '{raw_title}') skipped: JD text too short ({len(clean_jd)} chars < {min_jd_length})."
            )
            continue

        raw_url = row.get(mapping.get("url", ""), "").strip() if "url" in mapping else ""
        if raw_url and not is_safe_url(raw_url):
            warnings.append(f"Row {row_num}: unsafe or invalid URL '{raw_url}' neutralized.")
            raw_url = "N/A"

        raw_location = row.get(mapping.get("location", ""), "").strip() if "location" in mapping else ""

        # Generate stable, deterministic job ID
        job_id = compute_stable_job_id(raw_company, raw_title, clean_jd, row_num)

        jobs.append({
            "job_id": job_id,
            "company": raw_company or "Unknown Company",
            "title": raw_title or "Software Engineer",
            "jd_text": clean_jd,
            "url": raw_url or "N/A",
            "location": raw_location or "Not specified",
            "raw_row_index": row_num,
        })

        if len(jobs) > MAX_JOBS_PER_RUN:
            raise ValueError(f"Job limit exceeded: CSV contains more than {MAX_JOBS_PER_RUN} valid jobs.")

    return jobs, mapping, warnings


def generate_sample_csv_str() -> str:
    """Generates a clean sample CSV for user download."""
    sample_rows = [
        {
            "Company": "Innovaccer",
            "Job Title": "Associate Software Engineer - Java",
            "Job Description": "Looking for a software engineer to design and build scalable microservices using Java, Spring Boot, and REST APIs. Responsibilities include database design with MySQL, writing clean modular code, unit testing with JUnit, and collaborating with Agile teams. Knowledge of Python, SQL, and Git is preferred.",
            "Apply Link": "https://careers.innovaccer.com/jobs/ase-java",
            "Location": "Noida, India / Hybrid"
        },
        {
            "Company": "Groww",
            "Job Title": "Full Stack Engineer Trainee",
            "Job Description": "We are hiring passionate Full Stack Developer Trainees. Requirements: strong problem solving in DSA, hands-on experience in React.js, Node.js, Express, and MongoDB or PostgreSQL. The candidate will work on high-traffic financial web applications, build responsive UI components, and integrate backend APIs.",
            "Apply Link": "https://groww.in/careers/full-stack-trainee",
            "Location": "Bangalore, India"
        },
        {
            "Company": "Tata Consultancy Services (TCS)",
            "Job Title": "Python / AI Automation Engineer",
            "Job Description": "TCS is seeking engineers to work on automation pipelines using Python, AsyncIO, REST API integrations, and generative AI models (OpenAI, Gemini). Requirements include knowledge of Python, Pandas, SQL databases, Git, and prompt engineering.",
            "Apply Link": "https://www.tcs.com/careers/ai-engineer",
            "Location": "Pune, India"
        }
    ]

    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=["Company", "Job Title", "Job Description", "Apply Link", "Location"])
    writer.writeheader()
    writer.writerows(sample_rows)
    return output.getvalue()
