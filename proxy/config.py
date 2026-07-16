import os
from pathlib import Path
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


class RewriteConfig(BaseModel):
    enabled: bool = False
    max_tokens_floor: MaxTokensFloorConfig
    explicit_thinking: ExplicitThinkingConfig
    message_canonicalization: MessageCanonicalizationConfig
    strict_format_guardrail: StrictFormatGuardrailConfig
    system_compression: SystemCompressionConfig


class ClassificationConfig(BaseModel):
    enabled: bool = True
    min_chars: int = Field(ge=1)
    min_line_breaks: int = Field(ge=0)
    reasoning_keyword_patterns: list[str]
    output_constraint_patterns: list[str]
    code_marker_patterns: list[str]
    rewrite_score_threshold: int = Field(ge=0)
    normalize_only_score_threshold: int = Field(ge=0)


class ProxyConfig(BaseModel):
    server: ServerConfig
    upstream: UpstreamConfig
    logging: LoggingConfig
    rewrite: RewriteConfig
    classification: ClassificationConfig


def load_config(path: str | Path) -> ProxyConfig:
    with Path(path).open("rb") as handle:
        raw = tomllib.load(handle)
    return ProxyConfig.model_validate(raw)


def validate_runtime_config(config: ProxyConfig) -> None:
    if config.classification.rewrite_score_threshold < config.classification.normalize_only_score_threshold:
        raise ValueError("rewrite_score_threshold must be >= normalize_only_score_threshold")
    parsed = urlparse(config.upstream.base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("upstream.base_url must be absolute http or https")
    if any(code < 400 or code > 599 for code in config.upstream.retry_statuses):
        raise ValueError("retry_statuses must contain only 4xx or 5xx codes")
    if config.logging.allow_raw_payload_logging:
        override_value = os.environ.get(config.logging.unsafe_override_via_env)
        if override_value != "YES_I_ACCEPT_RAW_LOGGING_RISK":
            raise ValueError(
                "raw payload logging requires the unsafe override value YES_I_ACCEPT_RAW_LOGGING_RISK",
            )


def validate_no_self_target(config: ProxyConfig) -> None:
    parsed = urlparse(config.upstream.base_url)
    if not config.upstream.allow_self_target and parsed.hostname == config.server.host and parsed.port == config.server.port:
        raise ValueError("upstream.base_url must not target the local listener")
