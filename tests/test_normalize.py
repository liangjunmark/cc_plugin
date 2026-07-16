from proxy.normalize import anthropic_error, filter_forward_headers, redact_headers


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
