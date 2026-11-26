from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class ScanRequest(BaseModel):
    url: str = Field(min_length=1, max_length=512)


class Location(BaseModel):
    path: str
    line_start: Optional[int] = None
    column_start: Optional[int] = None
    line_end: Optional[int] = None
    column_end: Optional[int] = None


class Finding(BaseModel):
    id: str
    rule_id: str
    rule_version: str
    dimension: str
    severity: str
    confidence: str
    title: str
    message: str
    location: Optional[Location] = None
    evidence: str
    proposed_deduction: int
    applied_deduction: int = 0
    deduction_reason: str = ""
    related_finding_id: Optional[str] = None
    recommendation: str
    source_url: Optional[str] = None


class Report(BaseModel):
    schema_version: str = "1.0.0"
    scan_id: str
    repository: Dict[str, str]
    commit_sha: str
    engine_version: str
    rules_digest: str
    status: str
    score: int
    score_kind: str
    dimensions: Dict[str, Any]
    checks: Dict[str, Any]
    findings: List[Finding]
    deductions: List[Dict[str, Any]]
    coverage: Dict[str, Any]
    limitations: List[str]
    advice: Optional[Dict[str, Any]] = None
