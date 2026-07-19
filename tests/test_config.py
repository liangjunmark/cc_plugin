from pathlib import Path

import pytest

from proxy.config import load_config, validate_no_self_target, validate_runtime_config


def write_config(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(body, encoding="utf-8")
    return path


def valid_config_body() -> str:
    return """
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

        [rewrite.premise_binding_guardrail]
        enabled = true

        [classification]
        enabled = true
        min_chars = 120
        min_line_breaks = 2
        reasoning_keyword_patterns = ["最少", "minimum"]
        output_constraint_patterns = ["只输出", "only output"]
        premise_control_patterns = ["手感", "靠手感", "touch", "distinguishable by touch", "区分形状"]
        code_marker_patterns = ["repo", "shell", "patch", "edit", "modify", "file", "python", "code", "function", "implement", "algorithm", "script", "\\\\.py\\\\b"]
        rewrite_score_threshold = 3
        normalize_only_score_threshold = 2

        [phase2]
        enabled = false
        trigger_on_routes = ["rewrite"]
        sample_count = 2
        max_adjudication_calls = 1
        max_parallelism = 2
        total_timeout_seconds = 180.0
        require_json_candidates = true
        candidate_roles = ["constraint_reasoner", "counterexample_reasoner"]
        max_candidate_output_tokens = 512
        max_total_upstream_calls = 4
        fallback_to_phase1_on_failure = true
        cost_budget_multiplier = 2.0
        allow_streaming_requests = false

        [phase2b]
        enabled = false
        trigger_on_routes = ["rewrite"]
        max_branch_count = 3
        branch_families = ["premise_first", "quota_first", "counterexample_first"]
        enable_assumption_audit = true
        enable_worst_case_attack = true
        enable_ledger_checks = true
        max_total_upstream_calls = 13
        total_timeout_seconds = 300.0
        allow_tiebreak_round = true
        require_exact_output_requests = true

        [phase2b.boundary_verifier]
        enabled = false
        lower_bound = 20
        upper_bound = 21
        trigger_markers = ["黑色的袋子", "手感可以分辨", "苹果味", "桃子味", "西瓜味", "圆形 7 9 8", "五角星形 7 6 4"]
        """


def test_example_config_is_loadable() -> None:
    config = load_config(Path("proxy/config.toml.example"))

    validate_runtime_config(config)


def test_load_config_accepts_valid_defaults(tmp_path: Path) -> None:
    path = write_config(tmp_path, valid_config_body())
    config = load_config(path)
    validate_runtime_config(config)
    assert config.server.port == 8787


def test_phase2_example_defaults_are_valid(tmp_path: Path) -> None:
    path = write_config(tmp_path, valid_config_body())

    config = load_config(path)
    validate_runtime_config(config)
    assert config.phase2.sample_count == 2
    assert config.phase2.allow_streaming_requests is False


def test_phase2b_defaults_are_valid(tmp_path: Path) -> None:
    path = write_config(tmp_path, valid_config_body())

    config = load_config(path)
    validate_runtime_config(config)
    assert config.phase2b.max_branch_count == 3
    assert config.phase2b.require_exact_output_requests is True
    assert config.phase2b.boundary_verifier.enabled is False


def test_phase2b_accepts_missing_boundary_verifier_block_when_disabled(tmp_path: Path) -> None:
    path = write_config(
        tmp_path,
        valid_config_body().replace(
            """
        [phase2b.boundary_verifier]
        enabled = false
        lower_bound = 20
        upper_bound = 21
        trigger_markers = ["黑色的袋子", "手感可以分辨", "苹果味", "桃子味", "西瓜味", "圆形 7 9 8", "五角星形 7 6 4"]
        """,
            "",
        ),
    )

    config = load_config(path)
    validate_runtime_config(config)
    assert config.phase2b.boundary_verifier.enabled is False


def test_phase2b_rejects_budget_below_enabled_stage_floor(tmp_path: Path) -> None:
    path = write_config(
        tmp_path,
        valid_config_body().replace("max_total_upstream_calls = 13", "max_total_upstream_calls = 11"),
    )

    config = load_config(path)
    with pytest.raises(ValueError, match="phase2b.max_total_upstream_calls"):
        validate_runtime_config(config)


def test_phase2b_rejects_budget_below_boundary_verifier_plus_enabled_stage_floor(tmp_path: Path) -> None:
    path = write_config(
        tmp_path,
        valid_config_body()
        .replace("enabled = false\n        lower_bound = 20", "enabled = true\n        lower_bound = 20")
        .replace("max_total_upstream_calls = 13", "max_total_upstream_calls = 13"),
    )

    config = load_config(path)
    with pytest.raises(ValueError, match="phase2b.max_total_upstream_calls"):
        validate_runtime_config(config)


@pytest.mark.parametrize(
    ("branch_families", "error"),
    [
        (
            '["premise_first", "premise_first", "counterexample_first"]',
            "phase2b.branch_families must be exactly",
        ),
        (
            '["premise_first", "quota_first", "unknown_family"]',
            "phase2b.branch_families must be exactly",
        ),
        (
            '["premise_first", "quota_first"]',
            "phase2b.branch_families must contain exactly 3 entries",
        ),
    ],
)
def test_phase2b_rejects_noncanonical_branch_families(
    tmp_path: Path,
    branch_families: str,
    error: str,
) -> None:
    path = write_config(
        tmp_path,
        valid_config_body().replace(
            'branch_families = ["premise_first", "quota_first", "counterexample_first"]',
            f"branch_families = {branch_families}",
        ),
    )

    with pytest.raises(ValueError, match=error):
        validate_runtime_config(load_config(path))


def test_phase2b_rejects_noncanonical_branch_count(tmp_path: Path) -> None:
    path = write_config(
        tmp_path,
        valid_config_body().replace("max_branch_count = 3", "max_branch_count = 2"),
    )

    with pytest.raises(ValueError, match="phase2b.max_branch_count must remain 3"):
        validate_runtime_config(load_config(path))


def test_phase2b_rejects_disabling_exact_output_requests(tmp_path: Path) -> None:
    path = write_config(
        tmp_path,
        valid_config_body().replace(
            "require_exact_output_requests = true",
            "require_exact_output_requests = false",
        ),
    )

    with pytest.raises(ValueError, match="phase2b.require_exact_output_requests must remain true"):
        validate_runtime_config(load_config(path))


def test_phase2b_boundary_verifier_rejects_non_adjacent_bounds(tmp_path: Path) -> None:
    path = write_config(
        tmp_path,
        valid_config_body()
        .replace("enabled = false\n        lower_bound = 20", "enabled = true\n        lower_bound = 20")
        .replace("upper_bound = 21", "upper_bound = 22"),
    )

    with pytest.raises(ValueError, match="phase2b.boundary_verifier upper_bound must equal lower_bound \\+ 1"):
        validate_runtime_config(load_config(path))


def test_phase2b_boundary_verifier_rejects_empty_trigger_markers(tmp_path: Path) -> None:
    path = write_config(
        tmp_path,
        valid_config_body()
        .replace("enabled = false\n        lower_bound = 20", "enabled = true\n        lower_bound = 20")
        .replace(
            'trigger_markers = ["黑色的袋子", "手感可以分辨", "苹果味", "桃子味", "西瓜味", "圆形 7 9 8", "五角星形 7 6 4"]',
            "trigger_markers = []",
        ),
    )

    with pytest.raises(ValueError, match="phase2b.boundary_verifier trigger_markers must not be empty when enabled"):
        validate_runtime_config(load_config(path))


def test_phase2b_boundary_verifier_rejects_disabling_xfyun_only_gate(tmp_path: Path) -> None:
    path = write_config(
        tmp_path,
        valid_config_body()
        .replace("enabled = false\n        lower_bound = 20", "enabled = true\n        lower_bound = 20")
        .replace("max_total_upstream_calls = 13", "max_total_upstream_calls = 14")
        .replace(
            'trigger_markers = ["黑色的袋子", "手感可以分辨", "苹果味", "桃子味", "西瓜味", "圆形 7 9 8", "五角星形 7 6 4"]',
            'require_xfyun_upstream = false\n        trigger_markers = ["黑色的袋子", "手感可以分辨", "苹果味", "桃子味", "西瓜味", "圆形 7 9 8", "五角星形 7 6 4"]',
        ),
    )

    with pytest.raises(ValueError, match="phase2b.boundary_verifier must remain xfyun-only"):
        validate_runtime_config(load_config(path))


def test_phase2_rejects_call_budget_that_cannot_cover_baseline_candidates_and_adjudication(
    tmp_path: Path,
) -> None:
    path = write_config(
        tmp_path,
        valid_config_body().replace("max_total_upstream_calls = 4", "max_total_upstream_calls = 3"),
    )

    config = load_config(path)
    with pytest.raises(ValueError, match="phase2.max_total_upstream_calls"):
        validate_runtime_config(config)


def test_phase2_rejects_call_budget_above_global_maximum(tmp_path: Path) -> None:
    path = write_config(
        tmp_path,
        valid_config_body().replace("max_total_upstream_calls = 4", "max_total_upstream_calls = 8"),
    )

    with pytest.raises(ValueError, match="less than or equal to 7"):
        load_config(path)


def test_phase2_rejects_disabling_strict_json_candidates(tmp_path: Path) -> None:
    path = write_config(
        tmp_path,
        valid_config_body().replace("require_json_candidates = true", "require_json_candidates = false"),
    )

    with pytest.raises(ValueError, match="phase2.require_json_candidates must remain true"):
        validate_runtime_config(load_config(path))


def test_phase2_rejects_disabling_phase1_failure_fallback(tmp_path: Path) -> None:
    path = write_config(
        tmp_path,
        valid_config_body().replace("fallback_to_phase1_on_failure = true", "fallback_to_phase1_on_failure = false"),
    )

    with pytest.raises(ValueError, match="phase2.fallback_to_phase1_on_failure must remain true"):
        validate_runtime_config(load_config(path))


def test_raw_payload_logging_requires_unsafe_override(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("CC_PROXY_UNSAFE_LOGGING", raising=False)
    path = write_config(
        tmp_path,
        valid_config_body().replace("allow_raw_payload_logging = false", "allow_raw_payload_logging = true"),
    )

    with pytest.raises(ValueError, match="YES_I_ACCEPT_RAW_LOGGING_RISK"):
        validate_runtime_config(load_config(path))


def test_raw_payload_logging_accepts_explicit_unsafe_override(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CC_PROXY_UNSAFE_LOGGING", "YES_I_ACCEPT_RAW_LOGGING_RISK")
    path = write_config(
        tmp_path,
        valid_config_body().replace("allow_raw_payload_logging = false", "allow_raw_payload_logging = true"),
    )

    validate_runtime_config(load_config(path))


def test_invalid_threshold_band_is_rejected(tmp_path: Path) -> None:
    path = write_config(
        tmp_path,
        valid_config_body()
        .replace('reasoning_keyword_patterns = ["最少", "minimum"]', 'reasoning_keyword_patterns = ["最少"]')
        .replace('output_constraint_patterns = ["只输出", "only output"]', 'output_constraint_patterns = ["只输出"]')
        .replace('code_marker_patterns = ["repo", "shell"]', 'code_marker_patterns = ["repo"]')
        .replace("rewrite_score_threshold = 3", "rewrite_score_threshold = 1"),
    )
    with pytest.raises(ValueError, match="rewrite_score_threshold"):
        validate_runtime_config(load_config(path))


@pytest.mark.parametrize(
    ("base_url", "server_port"),
    [
        ("http://127.0.0.1", 80),
        ("https://127.0.0.1", 443),
    ],
)
def test_validate_no_self_target_rejects_default_ports(
    base_url: str,
    server_port: int,
    tmp_path: Path,
) -> None:
    path = write_config(
        tmp_path,
        valid_config_body()
        .replace('host = "127.0.0.1"', 'host = "127.0.0.1"', 1)
        .replace('port = 8787', f"port = {server_port}", 1)
        .replace('base_url = "https://example.com/anthropic"', f'base_url = "{base_url}"', 1)
        .replace("allow_self_target = false", "allow_self_target = false", 1),
    )

    config = load_config(path)

    with pytest.raises(ValueError, match="must not target the local listener"):
        validate_no_self_target(config)


@pytest.mark.parametrize(
    ("server_host", "base_url"),
    [
        ("0.0.0.0", "http://127.0.0.1:8787"),
        ("LOCALHOST", "http://localhost:8787"),
    ],
)
def test_validate_no_self_target_rejects_equivalent_hosts(
    server_host: str,
    base_url: str,
    tmp_path: Path,
) -> None:
    path = write_config(
        tmp_path,
        valid_config_body()
        .replace('host = "127.0.0.1"', f'host = "{server_host}"', 1)
        .replace('base_url = "https://example.com/anthropic"', f'base_url = "{base_url}"', 1),
    )

    config = load_config(path)

    with pytest.raises(ValueError, match="must not target the local listener"):
        validate_no_self_target(config)


def test_validate_runtime_config_rejects_hostless_upstream_url(tmp_path: Path) -> None:
    path = write_config(
        tmp_path,
        valid_config_body().replace('base_url = "https://example.com/anthropic"', 'base_url = "https://user@"', 1),
    )

    with pytest.raises(ValueError, match="hostname"):
        validate_runtime_config(load_config(path))


def test_phase2_accepts_highest_valid_total_call_budget_configuration(tmp_path: Path) -> None:
    path = write_config(
        tmp_path,
        valid_config_body()
        .replace("sample_count = 2", "sample_count = 5", 1)
        .replace("candidate_roles = [\"constraint_reasoner\", \"counterexample_reasoner\"]", 'candidate_roles = ["constraint_reasoner", "counterexample_reasoner", "constraint_reasoner", "counterexample_reasoner", "constraint_reasoner"]', 1)
        .replace("max_parallelism = 2", "max_parallelism = 5", 1)
        .replace("max_adjudication_calls = 1", "max_adjudication_calls = 0", 1)
        .replace("max_total_upstream_calls = 4", "max_total_upstream_calls = 7", 1),
    )

    config = load_config(path)
    validate_runtime_config(config)
    assert config.phase2.max_total_upstream_calls == 7
