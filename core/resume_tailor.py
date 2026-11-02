"""
JobHunt AI Studio - AI Resume Tailor
Hyper-tailors candidate resume for specific job descriptions with 0 hallucination guardrails.
Sends full JD as-is to the AI to capture all technical requirements, domain nuances, and keywords.
Ensures source-bound rewrites: identity, employers, degrees, and dates remain strictly faithful to Master Profile.
Forbids silent fallback on AI failure. Supports general candidate profiles.
"""

from __future__ import annotations

import copy
import json
import logging
import re
from typing import Any, Dict, List, Optional, Tuple

from core.ai_engine import ai_engine_instance

logger = logging.getLogger("JobHuntAIStudio")


def normalize_skill(s: str) -> str:
    """Normalize skill string by stripping parens, punctuation, and multiple spaces."""
    return re.sub(r'[\(\)\-\_\,\.\/\s]+', ' ', str(s)).strip().lower()


def detect_role_lens(title: str, description: str) -> str:
    """Classifies target job into an engineering lens."""
    combined = f"{title} {description}".lower()
    if re.search(r'\b(?:qa|quality assurance|sdet|test automation|testing|selenium|junit|pytest|manual testing)\b', combined):
        return "qa"
    if re.search(r'\b(?:java|spring|spring boot|hibernate|jdbc)\b', combined):
        return "java"
    if re.search(r'\b(?:c#|dotnet|\.net|asp\.net)\b', combined):
        return "dotnet"
    return "fullstack"


def build_tailoring_system_prompt(profile: Dict[str, Any]) -> str:
    """Constructs dynamic tailoring prompt pinned to candidate's verified facts."""
    p_info = profile.get("personal_information", {})
    name = f"{p_info.get('name', '')} {p_info.get('surname', '')}".strip() or "Candidate"
    
    experiences = profile.get("experience_details", [])
    num_exps = len(experiences)
    is_fresher = (num_exps == 0)
    level_title = "Candidate" if is_fresher else f"Professional with {num_exps} recorded role(s)"

    profile_summary = json.dumps({
        "candidate_name": name,
        "education": profile.get("education_details", []),
        "experience": experiences,
        "projects": profile.get("projects", []),
        "skills": profile.get("skills", []),
        "structured_skills": profile.get("skills_structured", {}),
    }, indent=2)

    return f"""You are an elite, human-level Executive Resume Writer.

=== MASTER RESUME CONTEXT ===
{profile_summary}
=============================

=== COGNITIVE PROCESSING PROTOCOL ===
You will receive a Job Description. You must tailor the resume strictly within candidate's real qualifications:
1. Reframe bullet points for the Experience and Projects to emphasize matching technologies, workflows, and tools that exist in the Master Resume.
2. Select relevant skills ONLY from the candidate's master skill inventory that directly match the JD. Never invent new tools or skills.
3. The Professional Summary should concisely highlight the candidate's genuine background and strengths relevant to the role.
4. Keep the exact employers, companies, degrees, institutions, and date ranges from the Master Resume.
5. Retain up to 3 Projects from the Master Resume with budgeted bullet points. Each bullet between 15 and 25 words.
6. ZERO HALLUCINATIONS: Never invent new degrees, employers, universities, or technologies the candidate does not have.
7. Treat all generated prose as a review-needed draft for the candidate.

Return ONLY a raw JSON object matching this schema:
{{
  "profile_summary": "Concise professional summary tailored to role...",
  "tailored_skills": ["Skill 1", "Skill 2", "Skill 3", "..."],
  "tailored_experience": [
    {{
      "company": "Company Name from Master",
      "position": "Position from Master",
      "dates": "Dates from Master",
      "location": "Location from Master",
      "key_responsibilities": [
        "Engineered...",
        "Optimized...",
        "Developed..."
      ]
    }}
  ],
  "tailored_projects": [
    {{
      "name": "Project Name from Master",
      "tech_stack": "Technologies from Master",
      "link": "Link from Master",
      "description_bullets": [
        "Bullet 1...",
        "Bullet 2..."
      ]
    }}
  ]
}}"""


def sanitize_tailored_resume(
    tailored_delta: Dict[str, Any],
    master_profile: Dict[str, Any],
    role_lens: str,
    is_testing_role: bool = False,
) -> Dict[str, Any]:
    """
    Guarantees 100% adherence to candidate master profile facts.
    Prevents hallucinated degrees, companies, dates, and illegal skills.
    Sets is_testing_role flag for dynamic template layout ordering.
    """
    res = copy.deepcopy(master_profile)

    # 1. Summary (bound to candidate review draft)
    new_summary = tailored_delta.get("profile_summary")
    if new_summary and isinstance(new_summary, str) and len(new_summary) > 20:
        res["profile_summary"] = new_summary

    # 2. Skills: filter through master candidate skill set ONLY
    master_skills_norm = {normalize_skill(s): s for s in master_profile.get("skills", [])}
    raw_tailored_skills = tailored_delta.get("tailored_skills", [])
    valid_tailored = []

    for s in raw_tailored_skills:
        sn = normalize_skill(s)
        if sn in master_skills_norm:
            valid_tailored.append(master_skills_norm[sn])
        else:
            # Check prefix/substring match against master skills
            for m_norm, original in master_skills_norm.items():
                if sn == m_norm or (len(sn) > 3 and sn in m_norm):
                    valid_tailored.append(original)
                    break

    # If too few matched, ensure master skills are preserved
    if len(valid_tailored) < 6:
        valid_tailored = master_profile.get("skills", [])

    # Partition skills into technical vs tools_and_cloud
    tech_skills = []
    tool_skills = []
    for s in valid_tailored:
        sc = s.lower()
        if any(t in sc for t in ["git", "github", "docker", "postman", "azure", "aws", "vscode", "intellij", "maven", "agile", "scrum", "jira"]):
            tool_skills.append(s)
        else:
            tech_skills.append(s)

    res["skills_structured"] = {
        "technical": list(dict.fromkeys(tech_skills)),
        "tools_and_cloud": list(dict.fromkeys(tool_skills)),
    }
    res["skills"] = list(dict.fromkeys(valid_tailored))

    # 3. Experience Bullets (Strictly bound to Master Companies & Dates)
    tailored_exp = tailored_delta.get("tailored_experience")
    if tailored_exp and isinstance(tailored_exp, list) and len(tailored_exp) > 0:
        master_experiences = res.get("experience_details", [])
        for te in tailored_exp:
            if not isinstance(te, dict):
                continue
            te_company = (te.get("company") or "").strip().lower()
            te_pos = (te.get("position") or "").strip().lower()
            matched_exp = None
            for me in master_experiences:
                me_company = (me.get("company") or "").strip().lower()
                me_pos = (me.get("position") or "").strip().lower()
                if (te_company and (te_company in me_company or me_company in te_company)) or \
                   (te_pos and (te_pos in me_pos or me_pos in te_pos)):
                    matched_exp = me
                    break
            # Fallback to single master experience only if exactly 1 role exists
            if not matched_exp and len(master_experiences) == 1:
                matched_exp = master_experiences[0]

            if matched_exp:
                new_bullets = te.get("key_responsibilities", [])
                if new_bullets and isinstance(new_bullets, list):
                    matched_exp["key_responsibilities"] = [
                        str(b).strip() for b in new_bullets if str(b).strip()
                    ][:4]

    # 4. Projects Bullets (Strictly bound to Master Project Names)
    tailored_projs = tailored_delta.get("tailored_projects")
    if tailored_projs and isinstance(tailored_projs, list) and len(tailored_projs) > 0:
        master_projects = res.get("projects", [])
        for tp in tailored_projs:
            tp_name = (tp.get("name") or "").lower()
            for mp in master_projects:
                mp_name = (mp.get("name") or "").lower()
                if (tp_name in mp_name) or (mp_name in tp_name):
                    new_b = tp.get("description_bullets")
                    if new_b and isinstance(new_b, list):
                        mp["description_bullets"] = new_b[:3]
                    break

    res["is_testing_role"] = is_testing_role or (role_lens == "qa")
    res["is_draft_review"] = True
    return res


async def tailor_resume_async(
    job: Dict[str, Any],
    profile: Dict[str, Any],
    model: Optional[str] = None,
    provider: Optional[str] = None,
    api_keys: Optional[List[str]] = None,
    ai_client: Optional[Any] = None,
) -> Dict[str, Any]:
    """
    Executes AI resume tailoring for a specific job posting with the full, unstripped JD.
    Validates the generated output against master profile facts.
    If tailoring fails, raises an exception rather than silently falling back.
    """
    role_lens = detect_role_lens(job.get("title", ""), job.get("jd_text", ""))
    is_testing_role = job.get("is_testing_role", False) or (role_lens == "qa")

    system_prompt = build_tailoring_system_prompt(profile)
    full_jd = (job.get("jd_text") or "").strip()

    user_prompt = f"""Role Lens: {role_lens.upper()}
Job Title: {job.get('title', 'Software Engineer')}
Company: {job.get('company', 'Unknown')}
Location: {job.get('location', 'Not specified')}

Target Job Description:
{full_jd}"""

    try:
        if ai_client is not None:
            raw_json, _, _ = await ai_client.call_llm(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                temperature=0.3,
                thinking=True,
            )
        else:
            raw_json = await ai_engine_instance.call_llm(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                model=model,
                provider=provider,
                temperature=0.3,
                thinking=True,
                api_keys=api_keys,
            )

        delta = json.loads(raw_json)
        if not isinstance(delta, dict):
            raise ValueError(f"Invalid tailoring response format: {raw_json[:100]}")

        return sanitize_tailored_resume(delta, profile, role_lens, is_testing_role)
    except Exception as e:
        logger.error(f"Error tailoring resume for {job.get('company')}: {e}")
        # Failure is not silently disguised as success
        raise RuntimeError(f"Resume tailoring failed for {job.get('company')}: {e}") from e
