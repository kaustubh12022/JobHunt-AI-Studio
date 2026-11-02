"""
JobHunt AI Studio - Core Data Models and Typed Contracts
Defines typed domain models for job inputs, run configurations, job execution results,
stage event telemetry, and artifact packaging.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class RunStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    AWAITING_CREDENTIALS = "awaiting_credentials"
    EXPIRED = "expired"
    CANCELLED = "cancelled"


class Stage(str, Enum):
    VALIDATE = "Validate"
    SCORE = "Score"
    TAILOR = "Tailor"
    RENDER = "Render"
    CHECK = "Check"
    PACKAGE = "Package"


@dataclass
class JobInput:
    job_id: str
    company: str
    title: str
    jd_text: str
    url: str = ""
    location: str = ""
    raw_row_index: int = 0
    fingerprint: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "job_id": self.job_id,
            "company": self.company,
            "title": self.title,
            "jd_text": self.jd_text,
            "url": self.url,
            "location": self.location,
            "raw_row_index": self.raw_row_index,
            "fingerprint": self.fingerprint,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> JobInput:
        return cls(
            job_id=data["job_id"],
            company=data.get("company", "Unknown"),
            title=data.get("title", "Position"),
            jd_text=data.get("jd_text", ""),
            url=data.get("url", ""),
            location=data.get("location", ""),
            raw_row_index=data.get("raw_row_index", 0),
            fingerprint=data.get("fingerprint", ""),
        )


@dataclass
class JobResult:
    job_id: str
    company: str
    title: str
    url: str = ""
    location: str = ""
    raw_row_index: int = 0
    match_score: int = 0
    reason: str = ""
    job_summary: str = ""
    missing_skills: List[str] = field(default_factory=list)
    extracted_requirements: str = ""
    is_testing_role: bool = False
    scoring_status: str = "pending"  # "success", "failed", "skipped"
    tailored_resume: Optional[Dict[str, Any]] = None
    pdf_path: Optional[str] = None
    pdf_filename: Optional[str] = None
    status: str = "Evaluated"
    audit_result: Optional[Dict[str, Any]] = None
    error: Optional[str] = None
    prompt_tokens: int = 0
    completion_tokens: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "job_id": self.job_id,
            "company": self.company,
            "title": self.title,
            "url": self.url,
            "location": self.location,
            "raw_row_index": self.raw_row_index,
            "match_score": self.match_score,
            "reason": self.reason,
            "job_summary": self.job_summary,
            "missing_skills": list(self.missing_skills),
            "extracted_requirements": self.extracted_requirements,
            "is_testing_role": self.is_testing_role,
            "scoring_status": self.scoring_status,
            "tailored_resume": self.tailored_resume,
            "pdf_path": self.pdf_path,
            "pdf_filename": self.pdf_filename,
            "status": self.status,
            "audit_result": self.audit_result,
            "error": self.error,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> JobResult:
        return cls(
            job_id=data["job_id"],
            company=data.get("company", "Unknown"),
            title=data.get("title", "Position"),
            url=data.get("url", ""),
            location=data.get("location", ""),
            raw_row_index=data.get("raw_row_index", 0),
            match_score=data.get("match_score", 0),
            reason=data.get("reason", ""),
            job_summary=data.get("job_summary", ""),
            missing_skills=data.get("missing_skills", []),
            extracted_requirements=data.get("extracted_requirements", ""),
            is_testing_role=data.get("is_testing_role", False),
            scoring_status=data.get("scoring_status", "pending"),
            tailored_resume=data.get("tailored_resume"),
            pdf_path=data.get("pdf_path"),
            pdf_filename=data.get("pdf_filename"),
            status=data.get("status", "Evaluated"),
            audit_result=data.get("audit_result"),
            error=data.get("error"),
            prompt_tokens=data.get("prompt_tokens", 0),
            completion_tokens=data.get("completion_tokens", 0),
        )


@dataclass
class RunConfig:
    run_id: str
    provider: str
    model: str
    score_threshold: int = 70
    max_resumes: int = 20
    is_gemini_free: bool = False
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "run_id": self.run_id,
            "provider": self.provider,
            "model": self.model,
            "score_threshold": self.score_threshold,
            "max_resumes": self.max_resumes,
            "is_gemini_free": self.is_gemini_free,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> RunConfig:
        return cls(
            run_id=data["run_id"],
            provider=data["provider"],
            model=data["model"],
            score_threshold=data.get("score_threshold", 70),
            max_resumes=data.get("max_resumes", 20),
            is_gemini_free=data.get("is_gemini_free", False),
            created_at=data.get("created_at", time.time()),
        )


@dataclass
class StageEvent:
    event_id: str
    run_id: str
    stage: str
    status: str  # "start", "progress", "success", "failed", "skipped"
    message: str
    job_id: Optional[str] = None
    timestamp: float = field(default_factory=time.time)
    details: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "event_id": self.event_id,
            "run_id": self.run_id,
            "job_id": self.job_id,
            "stage": self.stage,
            "status": self.status,
            "message": self.message,
            "timestamp": self.timestamp,
            "details": self.details,
        }


@dataclass
class ArtifactResult:
    run_id: str
    zip_path: Optional[str] = None
    zip_bytes: Optional[bytes] = None
    summary_csv_str: Optional[str] = None
    summary_html_str: Optional[str] = None
    pdf_paths: List[str] = field(default_factory=list)
    verified_count: int = 0
    failed_count: int = 0
