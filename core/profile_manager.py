"""
JobHunt AI Studio - Profile Manager
Loads, validates, and normalizes the single owner profile (Kaustubh Kale).
The profile is bundled with the repository and is not editable from the web UI.
"""

from __future__ import annotations

import copy
import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

import yaml

from core.security import MAX_PROFILE_BYTES
from core.settings import PROFILE_PATH

logger = logging.getLogger("JobHuntAIStudio")


def load_profile_from_str(content: str) -> Dict[str, Any]:
    """Parse profile from raw string (YAML or JSON) enforcing size limits."""
    if len(content.encode("utf-8")) > MAX_PROFILE_BYTES:
        raise ValueError(f"Profile content exceeds 1 MB limit ({len(content.encode('utf-8'))} bytes).")

    cleaned = content.strip()
    if not cleaned:
        raise ValueError("Profile content is empty.")
    try:
        data = yaml.safe_load(cleaned)
        if isinstance(data, dict):
            return normalize_profile(data)
    except Exception:
        pass
    try:
        data = json.loads(cleaned)
        if isinstance(data, dict):
            return normalize_profile(data)
    except Exception as e:
        raise ValueError(f"Failed to parse profile content as YAML or JSON: {e}")
    raise ValueError("Invalid profile format: root element must be a dictionary.")


def load_profile_from_file(path_or_buf: Union[str, Path]) -> Dict[str, Any]:
    """Load and validate profile from file path."""
    p = Path(path_or_buf)
    if not p.exists():
        raise FileNotFoundError(f"Profile file not found: {p}")
    with open(p, "r", encoding="utf-8") as f:
        return load_profile_from_str(f.read())


def load_owner_profile() -> Dict[str, Any]:
    """Return the locked studio owner profile."""
    return load_profile_from_file(PROFILE_PATH)


def normalize_profile(raw: Dict[str, Any]) -> Dict[str, Any]:
    """
    Normalizes the owner profile:
    - Guarantees schema types without inventing unsupplied skills, locations, degrees, or dates.
    - Preserves every skill, project, certification and achievement supplied in the file.
    """
    prof = copy.deepcopy(raw or {})

    # 1. Personal Information
    pi = prof.get("personal_information") or {}
    if not isinstance(pi, dict):
        pi = {}

    address_val = pi.get("address") or pi.get("location") or ""
    prof["personal_information"] = {
        "name": pi.get("name") or "Candidate",
        "surname": pi.get("surname") or "",
        "email": pi.get("email") or "",
        "phone": pi.get("phone") or "",
        "address": address_val,
        "city": pi.get("city") or "",
        "state": pi.get("state") or "",
        "country": pi.get("country") or "",
        "linkedin": pi.get("linkedin") or "",
        "github": pi.get("github") or "",
    }

    # 2. Summary
    prof["profile_summary"] = (
        prof.get("profile_summary")
        or prof.get("professional_summary")
        or prof.get("summary")
        or ""
    )

    # 3. Education
    edu = prof.get("education_details") or prof.get("education") or []
    if isinstance(edu, dict):
        edu = [edu]
    clean_edu = []
    for item in edu:
        if isinstance(item, dict):
            dates_val = item.get("dates")
            if not dates_val:
                s_date = item.get("start_date")
                e_date = item.get("year_of_completion") or item.get("end_date")
                if s_date and e_date:
                    dates_val = f"{s_date} - {e_date}"
                elif e_date:
                    dates_val = str(e_date)
                else:
                    dates_val = ""

            clean_edu.append({
                "institution": item.get("institution") or item.get("college") or item.get("school") or "",
                "education_level": item.get("education_level") or item.get("degree") or item.get("course") or "",
                "location": item.get("location") or "",
                "dates": dates_val,
                "final_evaluation_grade": item.get("final_evaluation_grade") or item.get("grade") or item.get("cgpa") or "",
                "secondary_education": item.get("secondary_education") or [],
            })
    prof["education_details"] = clean_edu

    # 4. Experience
    exp = prof.get("experience_details") or prof.get("work_experience") or prof.get("experience") or []
    if isinstance(exp, dict):
        exp = [exp]
    clean_exp = []
    for item in exp:
        if isinstance(item, dict):
            resps = item.get("key_responsibilities") or item.get("responsibilities") or item.get("bullets") or []
            clean_resps: List[str] = []
            if isinstance(resps, list):
                for r in resps:
                    if isinstance(r, dict):
                        clean_resps.extend(str(v) for v in r.values())
                    elif isinstance(r, str):
                        clean_resps.append(r)
            clean_exp.append({
                "company": item.get("company") or item.get("employer") or "",
                "position": item.get("position") or item.get("role") or item.get("title") or "",
                "dates": item.get("dates") or item.get("employment_period") or "",
                "location": item.get("location") or "",
                "key_responsibilities": clean_resps,
            })
    prof["experience_details"] = clean_exp

    # 5. Projects
    projects = prof.get("projects") or prof.get("technical_projects") or []
    if isinstance(projects, dict):
        projects = [projects]
    clean_proj = []
    for item in projects:
        if isinstance(item, dict):
            bullets = item.get("description_bullets") or item.get("bullets") or []
            if not bullets and item.get("description"):
                desc = item["description"]
                if isinstance(desc, list):
                    bullets = desc
                elif isinstance(desc, str):
                    bullets = [b.strip() for b in desc.split(". ") if b.strip()]
            clean_proj.append({
                "name": item.get("name") or item.get("title") or "Project",
                "tech_stack": item.get("tech_stack") or item.get("technologies") or item.get("tech_tag") or "",
                "link": item.get("link") or item.get("github") or "",
                "description_bullets": bullets,
            })
    prof["projects"] = clean_proj

    # 6. Skills
    skills_raw = prof.get("skills") or []
    if isinstance(skills_raw, dict):
        flat_skills: List[Any] = []
        for v in skills_raw.values():
            if isinstance(v, list):
                flat_skills.extend(v)
        skills_raw = flat_skills
    prof["skills"] = [str(s).strip() for s in skills_raw if str(s).strip()]

    # 7. Structured skills (preserved verbatim when supplied)
    struct = prof.get("skills_structured") or {}
    if not isinstance(struct, dict) or not struct:
        tech, tools = [], []
        for s in prof["skills"]:
            sc = s.lower()
            if any(t in sc for t in ["git", "github", "docker", "postman", "azure", "aws", "vscode", "intellij", "maven", "agile", "scrum", "jira"]):
                tools.append(s)
            else:
                tech.append(s)
        struct = {"technical": tech, "tools_and_cloud": tools}
    prof["skills_structured"] = struct

    # 8. Certifications & Achievements
    certs = prof.get("certifications") or []
    prof["certifications"] = certs if isinstance(certs, list) else [certs]

    achs = prof.get("achievements") or []
    prof["achievements"] = achs if isinstance(achs, list) else [achs]

    # 9. Target Roles
    prof["target_roles"] = prof.get("target_roles") or ["Software Engineer"]

    # 10. Optional owner extras (kept if supplied)
    for optional in ("languages", "work_preferences"):
        if optional in raw:
            prof[optional] = raw[optional]

    return prof


def profile_to_yaml_str(profile: Dict[str, Any]) -> str:
    """Serialize profile dict to clean YAML string."""
    return yaml.dump(profile, sort_keys=False, allow_unicode=True, indent=2)
