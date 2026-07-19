import ipaddress
import os
from pathlib import Path
import re
from urllib.parse import urlparse
import tomllib

from pydantic import BaseModel, Field


class ServerConfig(BaseModel):
    host: str
    port: int = Field(ge=1, le=65535)
    request_log_dir: str


class UpstreamConfig(BaseModel):
    base_url: str
    api_key_env: str
    timeout_seconds: float = Field(gt=0.0, le=600.0)
    connect_timeout_seconds: float = Field(gt=0.0, le=600.0)
    max_retries: int = Field(ge=0, le=1)
    retry_statuses: list[int]
    stream_probe_on_startup: bool
    allow_self_target: bool = False


class LoggingConfig(BaseModel):
    redact_secrets: bool = True
    allow_raw_payload_logging: bool = False
    unsafe_override_via_env: str
    redaction_whitelist: list[str] = []


class MaxTokensFloorConfig(BaseModel):
    enabled: bool = True
    minimum_output_tokens: int = Field(ge=1)


class ExplicitThinkingConfig(BaseModel):
    enabled: bool = False
    inject_when_missing: bool = False
    minimum_budget_tokens: int = Field(ge=1)


class MessageCanonicalizationConfig(BaseModel):
    enabled: bool = True


class StrictFormatGuardrailConfig(BaseModel):
    enabled: bool = True
    max_suffix_chars: int = Field(ge=1)


class SystemCompressionConfig(BaseModel):
    enabled: bool = False
    max_input_system_chars: int = Field(ge=1)
    target_system_chars: int = Field(ge=1)


class PremiseBindingGuardrailConfig(BaseModel):
    enabled: bool = True


class RewriteConfig(BaseModel):
    enabled: bool = False
    max_tokens_floor: MaxTokensFloorConfig
    explicit_thinking: ExplicitThinkingConfig
    message_canonicalization: MessageCanonicalizationConfig
    strict_format_guardrail: StrictFormatGuardrailConfig
    system_compression: SystemCompressionConfig
    premise_binding_guardrail: PremiseBindingGuardrailConfig = Field(
        default_factory=PremiseBindingGuardrailConfig
    )


class ClassificationConfig(BaseModel):
    enabled: bool = True
    min_chars: int = Field(ge=1)
    min_line_breaks: int = Field(ge=0)
    reasoning_keyword_patterns: list[str]
    output_constraint_patterns: list[str]
    premise_control_patterns: list[str] = Field(default_factory=list)
    code_marker_patterns: list[str]
    rewrite_score_threshold: int = Field(ge=0)
    normalize_only_score_threshold: int = Field(ge=0)


class Phase2Config(BaseModel):
    enabled: bool = False
    trigger_on_routes: list[str]
    sample_count: int = Field(ge=2, le=5)
    max_adjudication_calls: int = Field(ge=0, le=1)
    max_parallelism: int = Field(ge=1)
    total_timeout_seconds: float = Field(gt=0.0)
    require_json_candidates: bool = True
    candidate_roles: list[str]
    max_candidate_output_tokens: int = Field(ge=1)
    max_total_upstream_calls: int = Field(ge=1, le=7)
    fallback_to_phase1_on_failure: bool = True
    cost_budget_multiplier: float = Field(ge=1.0)
    allow_streaming_requests: bool = False


class Phase2bConfig(BaseModel):
    enabled: bool = False
    trigger_on_routes: list[str]
    max_branch_count: int = Field(ge=2, le=5)
    branch_families: list[str]
    enable_assumption_audit: bool = True
    enable_worst_case_attack: bool = True
    enable_ledger_checks: bool = True
    max_total_upstream_calls: int = Field(ge=1, le=20)
    total_timeout_seconds: float = Field(gt=0.0, le=900.0)
    allow_tiebreak_round: bool = True
    require_exact_output_requests: bool = True
    boundary_verifier: "Phase2bBoundaryVerifierConfig" = Field(default_factory=lambda: Phase2bBoundaryVerifierConfig())


class Phase2bBoundaryVerifierConfig(BaseModel):
    enabled: bool = False
    require_xfyun_upstream: bool = True
    lower_bound: int = 20
    upper_bound: int = 21
    trigger_markers: list[str] = Field(default_factory=list)


class ProxyConfig(BaseModel):
    server: ServerConfig
    upstream: UpstreamConfig
    logging: LoggingConfig
    rewrite: RewriteConfig
    classification: ClassificationConfig
    phase2: Phase2Config
    phase2b: Phase2bConfig


KNOWN_PHASE2_ROLES = {"constraint_reasoner", "counterexample_reasoner"}
PHASE2B_BRANCH_FAMILIES = {
    "premise_first",
    "quota_first",
    "counterexample_first",
}


def load_config(path: str | Path) -> ProxyConfig:
    with Path(path).open("rb") as handle:
        raw = tomllib.load(handle)
    return ProxyConfig.model_validate(raw)


def validate_runtime_config(config: ProxyConfig) -> None:
    if config.classification.rewrite_score_threshold < config.classification.normalize_only_score_threshold:
        raise ValueError("rewrite_score_threshold must be >= normalize_only_score_threshold")
    parsed = urlparse(config.upstream.base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or not parsed.hostname:
        raise ValueError("upstream.base_url must be an absolute http or https URL with a hostname")
    if any(code < 400 or code > 599 for code in config.upstream.retry_statuses):
        raise ValueError("retry_statuses must contain only 4xx or 5xx codes")
    if config.logging.allow_raw_payload_logging:
        override_value = os.environ.get(config.logging.unsafe_override_via_env)
        if override_value != "YES_I_ACCEPT_RAW_LOGGING_RISK":
            raise ValueError(
                "raw payload logging requires the unsafe override value YES_I_ACCEPT_RAW_LOGGING_RISK",
            )
    _validate_regex_patterns(config.classification.reasoning_keyword_patterns)
    _validate_regex_patterns(config.classification.output_constraint_patterns)
    _validate_regex_patterns(config.classification.premise_control_patterns)
    _validate_regex_patterns(config.classification.code_marker_patterns)
    validate_no_self_target(config)
    validate_phase2_config(config)
    validate_phase2b_config(config)


def validate_phase2_config(config: ProxyConfig) -> None:
    phase2 = config.phase2
    if not phase2.require_json_candidates:
        raise ValueError("phase2.require_json_candidates must remain true in the minimal design")
    if not phase2.fallback_to_phase1_on_failure:
        raise ValueError("phase2.fallback_to_phase1_on_failure must remain true in the minimal design")
    if phase2.max_parallelism > phase2.sample_count:
        raise ValueError("phase2.max_parallelism must be <= phase2.sample_count")
    if len(phase2.candidate_roles) != phase2.sample_count:
        raise ValueError("phase2.candidate_roles length must equal phase2.sample_count")
    if any(role not in KNOWN_PHASE2_ROLES for role in phase2.candidate_roles):
        raise ValueError("phase2.candidate_roles contains an unknown role")
    minimum_calls = 1 + phase2.sample_count + phase2.max_adjudication_calls
    if phase2.max_total_upstream_calls < minimum_calls:
        raise ValueError("phase2.max_total_upstream_calls must cover baseline, candidates, and adjudication")
    if phase2.allow_streaming_requests:
        raise ValueError("phase2.allow_streaming_requests must remain false in the minimal design")


def validate_phase2b_config(config: ProxyConfig) -> None:
    phase2b = config.phase2b
    if phase2b.max_branch_count != len(PHASE2B_BRANCH_FAMILIES):
        raise ValueError("phase2b.max_branch_count must remain 3 in the fixed topology")
    if len(phase2b.branch_families) != len(PHASE2B_BRANCH_FAMILIES):
        raise ValueError("phase2b.branch_families must contain exactly 3 entries")
    if set(phase2b.branch_families) != PHASE2B_BRANCH_FAMILIES:
        raise ValueError(
            "phase2b.branch_families must be exactly premise_first, quota_first, and counterexample_first"
        )
    if not phase2b.require_exact_output_requests:
        raise ValueError("phase2b.require_exact_output_requests must remain true in the fixed topology")
    minimum_calls = 1 + phase2b.max_branch_count
    if phase2b.boundary_verifier.enabled:
        if not phase2b.boundary_verifier.require_xfyun_upstream:
            raise ValueError("phase2b.boundary_verifier must remain xfyun-only in the current experiment")
        if phase2b.boundary_verifier.upper_bound != phase2b.boundary_verifier.lower_bound + 1:
            raise ValueError("phase2b.boundary_verifier upper_bound must equal lower_bound + 1")
        if not phase2b.boundary_verifier.trigger_markers:
            raise ValueError("phase2b.boundary_verifier trigger_markers must not be empty when enabled")
        # Reserve one extra slot so a verifier miss does not consume the full
        # ordinary phase2b safety margin before branch generation begins.
        minimum_calls += 2
    if phase2b.enable_assumption_audit:
        minimum_calls += phase2b.max_branch_count
    if phase2b.enable_worst_case_attack:
        minimum_calls += phase2b.max_branch_count
    if phase2b.allow_tiebreak_round:
        minimum_calls += 1
    minimum_calls += 1  # final compressor
    if phase2b.max_total_upstream_calls < minimum_calls:
        raise ValueError("phase2b.max_total_upstream_calls must cover baseline, enabled attack stages, and final compression")


def validate_no_self_target(config: ProxyConfig) -> None:
    parsed = urlparse(config.upstream.base_url)
    if (
        not config.upstream.allow_self_target
        and _effective_port(parsed.scheme, parsed.port) == config.server.port
        and _hosts_can_overlap(config.server.host, parsed.hostname)
    ):
        raise ValueError("upstream.base_url must not target the local listener")


def _effective_port(scheme: str, port: int | None) -> int | None:
    if port is not None:
        return port
    if scheme == "http":
        return 80
    if scheme == "https":
        return 443
    return None


def _hosts_can_overlap(server_host: str, upstream_host: str) -> bool:
    normalized_server = server_host.strip().lower()
    normalized_upstream = upstream_host.strip().lower()
    if normalized_server == normalized_upstream:
        return True
    if _is_unspecified_host(normalized_server) and _is_loopback_host(normalized_upstream):
        return True
    if _is_unspecified_host(normalized_upstream) and _is_loopback_host(normalized_server):
        return True
    return False


def _is_unspecified_host(host: str) -> bool:
    try:
        return ipaddress.ip_address(host).is_unspecified
    except ValueError:
        return False


def _is_loopback_host(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _validate_regex_patterns(patterns: list[str]) -> None:
    for pattern in patterns:
        try:
            re.compile(pattern, re.IGNORECASE)
        except re.error as exc:
            raise ValueError(f"invalid regex pattern: {pattern}") from exc
