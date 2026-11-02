"""
JobHunt AI Studio - Security & Input Safety Suite
Enforces strict size limits, safe URL protocols, CSV formula injection defense,
cryptographic recovery code hashing, input fingerprinting, and collision-free naming.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

# Strict system-wide constraints
MAX_CSV_BYTES = 5 * 1024 * 1024         # 5 MB
MAX_PROFILE_BYTES = 1 * 1024 * 1024     # 1 MB
MAX_JOBS_PER_RUN = 100                  # Max 100 jobs per batch
MAX_JD_CHARACTERS = 30_000              # Max characters in single JD (reject if exceeded)
MAX_ACTIVE_RUNS = 2                     # Max 2 concurrent active runs
MAX_QUEUED_RUNS = 10                    # Max 10 queued runs


def is_safe_url(url: Optional[str]) -> bool:
    """Validates that a URL uses strictly http or https schemes and contains a valid host."""
    if not url:
        return False
    u = str(url).strip()
    if not u or u.upper() == "N/A":
        return False
    try:
        parsed = urlparse(u)
        return parsed.scheme.lower() in ("http", "https") and bool(parsed.netloc)
    except Exception:
        return False


def sanitize_url(url: Optional[str]) -> str:
    """Returns the URL if safe, or an empty string if malicious/unsupported."""
    if is_safe_url(url):
        return str(url).strip()
    return ""


def neutralize_csv_cell(value: Any) -> str:
    """
    Prevents CSV Formula Injection (DDE attacks) in spreadsheet software (Excel, Calc).
    If a cell starts with '=', '+', '-', '@', tab, or carriage return, prepends with a single quote.
    """
    if value is None:
        return ""
    val_str = str(value)
    if val_str and val_str[0] in ("=", "+", "-", "@", "\t", "\r"):
        return f"'{val_str}"
    return val_str


def sanitize_filename_token(text: str, max_length: int = 30) -> str:
    """Produces clean alphanumeric filename fragments with underscores."""
    clean = re.sub(r'[^a-zA-Z0-9]+', '_', str(text)).strip('_')
    return clean[:max_length] if clean else "Item"


def compute_stable_job_id(company: str, title: str, jd_text: str, row_index: int = 0) -> str:
    """
    Generates a deterministic, stable job ID based on content hashing.
    Preserves identity across browser refreshes and recovery runs.
    """
    raw_payload = f"{company.strip().lower()}|{title.strip().lower()}|{jd_text.strip()[:300]}|{row_index}"
    digest = hashlib.sha256(raw_payload.encode("utf-8", errors="replace")).hexdigest()
    return f"job_{digest[:10]}"


def build_safe_pdf_filename(company: str, title: str, job_id: str, candidate_name: str) -> str:
    """
    Builds a collision-free PDF filename incorporating the stable job ID:
    {Company}_{Title}_{JobID}_{CandidateName}.pdf
    Guarantees that multiple jobs at the same company with the same title never overwrite each other.
    """
    c_part = sanitize_filename_token(company, 25)
    t_part = sanitize_filename_token(title, 25)
    j_part = sanitize_filename_token(job_id, 25)
    n_part = sanitize_filename_token(candidate_name, 25)
    return f"{c_part}_{t_part}_{j_part}_{n_part}.pdf"


def compute_input_fingerprint(
    jobs: List[Dict[str, Any]],
    profile: Dict[str, Any],
    threshold: int,
    max_resumes: int,
    model: str,
) -> str:
    """
    Generates a deterministic hash of the batch inputs and settings.
    Used by checkpoints to verify input identity before resuming.
    """
    job_signatures = [
        f"{j.get('job_id', '')}:{j.get('company', '')}:{j.get('title', '')}:{len(j.get('jd_text', ''))}"
        for j in jobs
    ]
    p_info = profile.get("personal_information", {})
    prof_sig = f"{p_info.get('name', '')}|{p_info.get('surname', '')}|{p_info.get('email', '')}|{len(profile.get('skills', []))}"
    combined = f"{'#'.join(job_signatures)}||{prof_sig}||t={threshold}||m={max_resumes}||model={model}"
    return hashlib.sha256(combined.encode("utf-8")).hexdigest()


def generate_recovery_code() -> str:
    """
    Generates a cryptographically strong, random recovery code.
    Format: jh-xxxx-xxxx-xxxx (12 lowercase alphanumeric characters).
    """
    part1 = secrets.token_hex(2)
    part2 = secrets.token_hex(2)
    part3 = secrets.token_hex(2)
    return f"jh-{part1}-{part2}-{part3}"


def hash_recovery_code(plain_code: str) -> str:
    """Hashes the recovery code using SHA-256 with consistent casing and trimming."""
    clean = plain_code.strip().lower()
    return hashlib.sha256(clean.encode("utf-8")).hexdigest()


def verify_recovery_code(plain_code: str, stored_hash: str) -> bool:
    """Constant-time verification of recovery code against its stored hash."""
    if not plain_code or not stored_hash:
        return False
    candidate_hash = hash_recovery_code(plain_code)
    return hmac.compare_digest(candidate_hash, stored_hash.strip().lower())
