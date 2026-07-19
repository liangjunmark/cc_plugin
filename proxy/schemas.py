from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from proxy.upstream import UpstreamResult


@dataclass(slots=True)
class RequestContext:
    request_id: str
    attempt: int
    log_dir: Path
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class NormalizedRequest:
    body: dict[str, Any]
    forward_headers: dict[str, str]
    recorder_headers: dict[str, str]


@dataclass(slots=True)
class CandidateAnswer:
    role: str
    raw_final_answer: str
    canonical_final_answer: str | None
    answer_type: str
    constraint_summary: list[str]
    critical_steps: list[str]
    counterexample_checked: bool
    confidence: str
    non_canonical: bool


@dataclass(slots=True)
class AggregationDecision:
    decision: str
    chosen_answer: str | None
    reason: str


@dataclass(slots=True)
class Phase2ExecutionResult:
    mode: str
    baseline_upstream: UpstreamResult
    candidates: list[CandidateAnswer]
    decision: AggregationDecision
    downstream_payload: dict[str, Any] | None


@dataclass(slots=True)
class Phase2bRawArtifact:
    stage: str
    role: str
    raw_text: str
    status_code: int
    observed_answer: str | None
    parse_quality: str


@dataclass(slots=True)
class Phase2bExtractedRecord:
    candidate_answer: str | None
    target_interpretation: str | None
    worst_case_claim: str | None
    controllable_premises: list[str]
    survives: bool | None
    fatal_error_tags: list[str]
    ledger_conflicts: list[str]
    revised_answer_if_any: str | None
    missing_fields: list[str]


@dataclass(slots=True)
class Phase2bBranchDecision:
    branch_id: str
    survived: bool
    fatal_error_tags: list[str]
    chosen_answer: str | None
    reason: str


@dataclass(slots=True)
class Phase2bExecutionResult:
    mode: str
    baseline_upstream: UpstreamResult
    branch_decisions: list[Phase2bBranchDecision]
    downstream_payload: dict[str, Any] | None
    fallback_reason: str | None
