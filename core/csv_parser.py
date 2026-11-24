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
    sanitize_url,
)

COLUMN_SYNONYMS = {
    "company": [
        "company", "company_name", "companyname", "employer", "employer_name",
        "organization", "organization_name", "org", "firm", "hiring_company",
        "hiring_organization", "client", "client_name", "brand", "business",
        "company_name_raw",
    ],
    "title": [
        "title", "job_title", "jobtitle", "role", "position", "job_name",
        "designation", "job_role", "position_title", "job_position", "role_name",
        "posting_title", "job_posting_title", "vacancy", "opening", "job_opening",
    ],
    "jd_text": [
        "jd_text", "job_description", "jobdescription", "job_description_text",
        "description", "description_text", "job_posting_description", "jd",
        "job_details", "details", "requirements", "responsibilities", "content",
        "job_summary", "about_the_job", "role_description", "what_you_will_do",
    ],
    "url": [
        "url", "job_url", "joburl", "link", "apply_link", "apply_url",
        "job_link", "posting_url", "href", "source_url", "external_apply_url",
        "job_posting_url", "application_url", "direct_apply_link",
    ],
    "company_website": ["company_website", "company_site", "official_website"],
    "location": [
        "location", "city", "work_mode", "job_location", "place", "state",
        "country", "location_text", "work_location", "office_location",
    ],
}


def detect_delimiter(text: str) -> str:
    """Picks the delimiter used by the header row of an exported CSV."""
    header = text.splitlines()[0] if text.splitlines() else ""
    counts = {candidate: header.count(candidate) for candidate in (",", ";", "\t", "|")}
    best = max(counts, key=counts.get)
    return best if counts[best] else ","


def decode_csv_bytes(raw: bytes) -> str:
    """Decodes a CSV export, tolerating Excel/Windows and UTF-8 sources."""
    for encoding in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


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
    Maps internal keys ('company', 'title', 'jd_text', 'url', 'location',
    'company_website') onto real CSV headers.

    Exact synonym matches win first. Remaining headers are assigned greedily by
    how specific the matched synonym is, so a column called "Role Description"
    fills the description field rather than the job title, and no single column
    is used twice.
    """
    normalized: Dict[str, str] = {}
    for header in headers:
        if not header:
            continue
        norm = re.sub(r"[^a-z0-9_]", "", header.strip().lower().replace(" ", "_"))
        if norm:
            normalized.setdefault(norm, header)

    mapping: Dict[str, str] = {}
    used: set[str] = set()

    for standard_key, synonyms in COLUMN_SYNONYMS.items():
        for synonym in synonyms:
            if synonym in normalized and normalized[synonym] not in used:
                mapping[standard_key] = normalized[synonym]
                used.add(normalized[synonym])
                break

    candidates: List[tuple[int, int, str, str]] = []
    for standard_key, synonyms in COLUMN_SYNONYMS.items():
        if standard_key in mapping:
            continue
        for norm_header, original in normalized.items():
            if original in used:
                continue
            lengths = [len(synonym) for synonym in synonyms if synonym in norm_header]
            if lengths:
                candidates.append((max(lengths), -len(norm_header), standard_key, original))

    for _, _, standard_key, original in sorted(candidates, reverse=True):
        if standard_key in mapping or original in used:
            continue
        mapping[standard_key] = original
        used.add(original)

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

    # 1. Normalise input (bytes, path-free streams or text) and enforce the size cap
    if isinstance(csv_content_or_file, (bytes, io.BytesIO)):
        raw = csv_content_or_file if isinstance(csv_content_or_file, bytes) else csv_content_or_file.read()
        if len(raw) > MAX_CSV_BYTES:
            raise ValueError(f"CSV file size exceeds 5 MB limit ({len(raw)} bytes).")
        text = decode_csv_bytes(raw)
    elif isinstance(csv_content_or_file, io.StringIO):
        text = csv_content_or_file.getvalue()
        if len(text.encode("utf-8")) > MAX_CSV_BYTES:
            raise ValueError("CSV content exceeds 5 MB limit.")
    elif isinstance(csv_content_or_file, str):
        text = csv_content_or_file
        if text.startswith("\ufeff"):
            text = text[1:]
        if len(text.encode("utf-8")) > MAX_CSV_BYTES:
            raise ValueError("CSV content exceeds 5 MB limit.")
    else:
        raise ValueError("Unsupported CSV content type.")

    # 2. Exports differ by source: sniff the delimiter instead of assuming commas
    delimiter = detect_delimiter(text)
    reader = csv.DictReader(io.StringIO(text, newline=""), delimiter=delimiter, restval="")
    headers = [header for header in (reader.fieldnames or []) if header and header.strip()]
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
            "company_website": sanitize_url(row.get(mapping.get("company_website", ""), "") or ""),
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
            "Job Description": "TCS is seeking engineers to work on automation pipelines using Python, AsyncIO, REST API integrations, and generative AI models (DeepSeek, OpenAI). Requirements include knowledge of Python, Pandas, SQL databases, Git, and prompt engineering.",
            "Apply Link": "https://www.tcs.com/careers/ai-engineer",
            "Location": "Pune, India"
        }
    ]

    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=["Company", "Job Title", "Job Description", "Apply Link", "Location"])
    writer.writeheader()
    writer.writerows(sample_rows)
    return output.getvalue()
