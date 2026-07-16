import json
from pathlib import Path

import pytest

from proxy.normalize import anthropic_error, filter_forward_headers, redact_headers
from proxy.recorder import Recorder
from proxy.schemas import RequestContext


def test_filter_forward_headers_strips_hop_by_hop_and_transport_headers() -> None:
    headers = {
        "connection": "keep-alive",
        "transfer-encoding": "chunked",
        "host": "localhost:8787",
        "content-length": "123",
        "anthropic-version": "2023-06-01",
        "accept-encoding": "gzip",
    }
    forwarded = filter_forward_headers(headers, safe_allowlist={"anthropic-version", "accept-encoding"})
    assert "connection" not in forwarded
    assert "transfer-encoding" not in forwarded
    assert "host" not in forwarded
    assert "content-length" not in forwarded
    assert forwarded["anthropic-version"] == "2023-06-01"


def test_anthropic_error_envelope_shape() -> None:
    payload = anthropic_error(502, "upstream failed", "api_error")
    assert payload["type"] == "error"
    assert payload["error"]["type"] == "api_error"
    assert payload["error"]["message"] == "upstream failed"


def test_redact_headers_denies_unknown_headers_by_default() -> None:
    headers = {"x-api-key": "secret", "anthropic-version": "2023-06-01"}
    redacted = redact_headers(headers, safe_allowlist={"anthropic-version"})
    assert redacted["x-api-key"] == "<redacted>"
    assert redacted["anthropic-version"] == "2023-06-01"


def test_redact_headers_never_whitelists_auth_headers() -> None:
    headers = {
        "Authorization": "Bearer secret",
        "x-api-key": "super-secret",
        "anthropic-version": "2023-06-01",
    }

    redacted = redact_headers(
        headers,
        safe_allowlist={"authorization", "x-api-key", "anthropic-version"},
    )

    assert redacted["Authorization"] == "<redacted>"
    assert redacted["x-api-key"] == "<redacted>"
    assert redacted["anthropic-version"] == "2023-06-01"


def test_redact_headers_never_whitelists_underscore_auth_headers() -> None:
    headers = {"X_API_KEY": "super-secret"}

    redacted = redact_headers(headers, safe_allowlist={"x_api_key"})

    assert redacted["X_API_KEY"] == "<redacted>"


def test_recorder_redacts_sensitive_dict_payloads_by_default(tmp_path: Path, config: object) -> None:
    recorder = Recorder(tmp_path, config)
    context = RequestContext(request_id="req-1", attempt=1, log_dir=Path("req-1"))

    recorder.write_artifact(
        context,
        "payload",
        {
            "authorization": "Bearer secret",
            "csrf_token": "token-value",
            "keyboard": "safe",
        },
    )

    payload = json.loads((tmp_path / "req-1" / "payload.json").read_text(encoding="utf-8"))
    assert payload["authorization"] == "<redacted>"
    assert payload["csrf_token"] == "<redacted>"
    assert payload["keyboard"] == "safe"


def test_recorder_redacts_github_pat_values_by_default(tmp_path: Path, config: object) -> None:
    recorder = Recorder(tmp_path, config)
    context = RequestContext(request_id="req-gh", attempt=1, log_dir=Path("req-gh"))

    recorder.write_artifact(
        context,
        "payload",
        {
            "message": "github_pat_1234567890secret",
            "nested": {"token": "ghp_1234567890secret"},
        },
    )

    payload = json.loads((tmp_path / "req-gh" / "payload.json").read_text(encoding="utf-8"))
    assert payload["message"] == "<redacted>"
    assert payload["nested"]["token"] == "<redacted>"


def test_recorder_blocks_raw_payload_logging_without_unsafe_override(
    tmp_path: Path,
    config: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config.logging.allow_raw_payload_logging = True
    monkeypatch.delenv(config.logging.unsafe_override_via_env, raising=False)
    recorder = Recorder(tmp_path, config)
    context = RequestContext(request_id="req-2", attempt=1, log_dir=Path("req-2"))

    recorder.write_artifact(context, "payload", "secret-body")

    assert (tmp_path / "req-2" / "payload.json").read_text(encoding="utf-8") == "<redacted>"


def test_recorder_allows_raw_payload_logging_with_unsafe_override(
    tmp_path: Path,
    config: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config.logging.allow_raw_payload_logging = True
    monkeypatch.setenv(config.logging.unsafe_override_via_env, "YES_I_ACCEPT_RAW_LOGGING_RISK")
    recorder = Recorder(tmp_path, config)
    context = RequestContext(request_id="req-3", attempt=1, log_dir=Path("req-3"))

    recorder.write_artifact(context, "payload", "secret-body")

    assert (tmp_path / "req-3" / "payload.json").read_text(encoding="utf-8") == "secret-body"


def test_recorder_rejects_paths_outside_root(tmp_path: Path, config: object) -> None:
    recorder = Recorder(tmp_path, config)
    context = RequestContext(request_id="req-4", attempt=1, log_dir=Path("../escape"))

    with pytest.raises(ValueError, match="recorder root"):
        recorder.write_artifact(context, "payload", {"safe": "value"})
