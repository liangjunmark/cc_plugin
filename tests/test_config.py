from pathlib import Path

import pytest

from proxy.config import load_config, validate_runtime_config


def write_config(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(body, encoding="utf-8")
    return path


def test_example_config_is_loadable() -> None:
    config = load_config(Path("proxy/config.toml.example"))

    validate_runtime_config(config)


def test_load_config_accepts_valid_defaults(tmp_path: Path) -> None:
    path = write_config(
        tmp_path,
        """
        [server]
        host = "127.0.0.1"
        port = 8787
        request_log_dir = "logs/requests"

        [upstream]
        base_url = "https://example.com/anthropic"
        api_key_env = "ANTHROPIC_AUTH_TOKEN"
        timeout_seconds = 60.0
        connect_timeout_seconds = 10.0
        max_retries = 1
        retry_statuses = [502, 503, 504]
        stream_probe_on_startup = true
        allow_self_target = false

        [logging]
        redact_secrets = true
        allow_raw_payload_logging = false
        unsafe_override_via_env = "CC_PROXY_UNSAFE_LOGGING"
        redaction_whitelist = []

        [rewrite]
        enabled = false

        [rewrite.max_tokens_floor]
        enabled = true
        minimum_output_tokens = 4096

        [rewrite.explicit_thinking]
        enabled = false
        inject_when_missing = false
        minimum_budget_tokens = 2048

        [rewrite.message_canonicalization]
        enabled = true

        [rewrite.strict_format_guardrail]
        enabled = true
        max_suffix_chars = 200

        [rewrite.system_compression]
        enabled = false
        max_input_system_chars = 8000
        target_system_chars = 2000

        [classification]
        enabled = true
        min_chars = 120
        min_line_breaks = 2
        reasoning_keyword_patterns = ["最少", "minimum"]
        output_constraint_patterns = ["只输出", "only output"]
        code_marker_patterns = ["repo", "shell"]
        rewrite_score_threshold = 4
        normalize_only_score_threshold = 2
        """,
    )
    config = load_config(path)
    validate_runtime_config(config)
    assert config.server.port == 8787


def test_invalid_threshold_band_is_rejected(tmp_path: Path) -> None:
    path = write_config(
        tmp_path,
        """
        [server]
        host = "127.0.0.1"
        port = 8787
        request_log_dir = "logs/requests"

        [upstream]
        base_url = "https://example.com/anthropic"
        api_key_env = "ANTHROPIC_AUTH_TOKEN"
        timeout_seconds = 60.0
        connect_timeout_seconds = 10.0
        max_retries = 1
        retry_statuses = [502, 503, 504]
        stream_probe_on_startup = true
        allow_self_target = false

        [logging]
        redact_secrets = true
        allow_raw_payload_logging = false
        unsafe_override_via_env = "CC_PROXY_UNSAFE_LOGGING"
        redaction_whitelist = []

        [rewrite]
        enabled = false

        [rewrite.max_tokens_floor]
        enabled = true
        minimum_output_tokens = 4096

        [rewrite.explicit_thinking]
        enabled = false
        inject_when_missing = false
        minimum_budget_tokens = 2048

        [rewrite.message_canonicalization]
        enabled = true

        [rewrite.strict_format_guardrail]
        enabled = true
        max_suffix_chars = 200

        [rewrite.system_compression]
        enabled = false
        max_input_system_chars = 8000
        target_system_chars = 2000

        [classification]
        enabled = true
        min_chars = 120
        min_line_breaks = 2
        reasoning_keyword_patterns = ["最少"]
        output_constraint_patterns = ["只输出"]
        code_marker_patterns = ["repo"]
        rewrite_score_threshold = 1
        normalize_only_score_threshold = 2
        """,
    )
    with pytest.raises(ValueError, match="rewrite_score_threshold"):
        validate_runtime_config(load_config(path))
