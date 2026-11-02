"""
JobHunt AI Studio - AI Relevance Scorer
Scores incoming job descriptions against candidate master profiles with zero invented fallback scores.
Sends full, complete job descriptions as-is to preserve all context, nuances, and technical criteria.
Supports general candidate profiles without hard-coding entry-level/fresher assumptions.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

from core.ai_engine import ai_engine_instance

logger = logging.getLogger("JobHuntAIStudio")


def build_scoring_system_prompt(profile: Dict[str, Any]) -> str:
    """Builds the ATS scoring system prompt dynamically bound to the candidate's master profile."""
    p_info = profile.get("personal_information", {})
    name = f"{p_info.get('name', '')} {p_info.get('surname', '')}".strip() or "Candidate"

    # Determine candidate seniority level from profile facts
    experiences = profile.get("experience_details", [])
    num_exps = len(experiences)
    is_fresher = (num_exps == 0)

    level_desc = "entry-level candidate" if is_fresher else f"candidate with {num_exps} recorded professional role(s)"

    profile_summary_json = json.dumps({
        "name": name,
        "education": profile.get("education_details", []),
        "experience": experiences,
        "projects": [
            {"name": p.get("name"), "tech_stack": p.get("tech_stack")}
            for p in profile.get("projects", [])
        ],
        "skills": profile.get("skills", []),
        "target_roles": profile.get("target_roles", [
            "Software Engineer", "Full Stack Developer", "Backend Developer"
        ]),
    }, indent=2)

    prompt = f"""You are a precise ATS scoring engine evaluating candidates against job requirements for a {level_desc}.

=== CANDIDATE MASTER RESUME (CACHED CONTEXT) ===
{profile_summary_json}
=================================================

=== SCORING RULES ===
- STRONG MATCH (75–95): Direct match on candidate's core stack, languages, tools, and experience level.
- MODERATE (60–75): General software roles with transferable OOP, SQL, or web fundamentals.
- LOW (25–50): Heavy DevOps/Cloud-only, hardware, or pure non-software roles.
- MISMATCH (0–20):
  * Out-of-scope technology stacks
  * Incompatible seniority level or mandatory prerequisites strictly absent from Master Profile
  * Non-technical roles (Sales, BPO, HR, Telecalling)

=== MISSING SKILLS ===
- Never report skills the candidate already possesses in their Master Profile as missing.
- Only report hard technical tools (e.g., "Kafka", "Kubernetes"), not soft skills.

Return ONLY a raw JSON object with exactly six keys:
- "match_score": integer 0-100
- "reason": concise explanation in EXACTLY 10 to 14 words.
- "job_summary": concise, single-sentence explanation of the role.
- "missing_skills": array of top 3-5 technical skills the candidate genuinely lacks.
- "extracted_requirements": concise comma-separated string of core technical stack.
- "is_testing_role": boolean (true if primarily QA, Testing, SDET, or Automation role, false otherwise).

CRITICAL: Return RAW JSON ONLY. No markdown wrappers or explanation."""
    return prompt


async def score_job_async(
    job: Dict[str, Any],
    profile: Dict[str, Any],
    model: Optional[str] = None,
    provider: Optional[str] = None,
    api_keys: Optional[List[str]] = None,
    ai_client: Optional[Any] = None,
) -> Dict[str, Any]:
    """
    Scores a single job against candidate profile using complete, unstripped JD.
    Validates typed scoring outputs. If AI scoring fails, flags as failed stage
    without inventing fabricated scores or fallback matches.
    """
    full_jd = (job.get("jd_text") or "").strip()
    system_prompt = build_scoring_system_prompt(profile)

    user_prompt = f"""Job Title: {job.get('title', 'Software Engineer')}
Company: {job.get('company', 'Unknown')}
Location: {job.get('location', 'Not specified')}

Full Job Description:
{full_jd}"""

    try:
        if ai_client is not None:
            raw_json, prompt_tok, comp_tok = await ai_client.call_llm(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                temperature=0.2,
                thinking=False,
            )
        else:
            raw_json = await ai_engine_instance.call_llm(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                model=model,
                provider=provider,
                temperature=0.2,
                thinking=False,
                api_keys=api_keys,
            )
            prompt_tok, comp_tok = 0, 0

        data = json.loads(raw_json)
        if not isinstance(data, dict) or "match_score" not in data:
            raise ValueError(f"Invalid scoring response schema from LLM: {raw_json[:120]}")

        match_score = int(data.get("match_score", 0))
        match_score = max(0, min(100, match_score))

        return {
            **job,
            "match_score": match_score,
            "reason": str(data.get("reason", "Evaluated against candidate qualifications.")),
            "job_summary": str(data.get("job_summary", "")),
            "missing_skills": list(data.get("missing_skills", [])),
            "extracted_requirements": str(data.get("extracted_requirements", "")),
            "is_testing_role": bool(data.get("is_testing_role", False)),
            "scoring_status": "success",
            "prompt_tokens": prompt_tok,
            "completion_tokens": comp_tok,
        }
    except Exception as e:
        logger.error(f"Error scoring job {job.get('company')} - {job.get('title')}: {e}")
        # Mark as failed stage without inventing scores or matches
        return {
            **job,
            "match_score": 0,
            "reason": f"Scoring failed: {str(e)[:60]}",
            "job_summary": "Scoring evaluation could not be completed.",
            "missing_skills": [],
            "extracted_requirements": "",
            "is_testing_role": False,
            "scoring_status": "failed",
            "status": f"Scoring Error: {str(e)[:40]}",
            "error": str(e),
        }
