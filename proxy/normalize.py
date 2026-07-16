from __future__ import annotations


HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}
TRANSPORT_REGENERATED_HEADERS = {"host", "content-length"}
SECRET_HEADER_FRAGMENTS = ("authorization", "api_key", "token", "secret")
ALWAYS_REDACT_HEADERS = {
    "authorization",
    "proxy_authorization",
    "x_api_key",
    "api_key",
    "cookie",
    "set_cookie",
}


def filter_forward_headers(headers: dict[str, str], safe_allowlist: set[str]) -> dict[str, str]:
    forwarded: dict[str, str] = {}
    connection_nominated = _connection_nominated_headers(headers)
    for name, value in headers.items():
        lower = name.lower()
        if (
            lower in HOP_BY_HOP_HEADERS
            or lower in TRANSPORT_REGENERATED_HEADERS
            or lower in connection_nominated
        ):
            continue
        forwarded[name] = value
    return forwarded


def redact_headers(headers: dict[str, str], safe_allowlist: set[str]) -> dict[str, str]:
    redacted: dict[str, str] = {}
    allow = {_canonicalize_header_name(name) for name in safe_allowlist}
    for name, value in headers.items():
        lower = _canonicalize_header_name(name)
        if lower in ALWAYS_REDACT_HEADERS or any(fragment in lower for fragment in SECRET_HEADER_FRAGMENTS):
            redacted[name] = "<redacted>"
        elif lower in allow:
            redacted[name] = value
        else:
            redacted[name] = "<redacted>"
    return redacted


def anthropic_error(status_code: int, message: str, error_type: str = "api_error") -> dict:
    return {"type": "error", "error": {"type": error_type, "message": message}, "status_code": status_code}


def _canonicalize_header_name(name: str) -> str:
    return name.strip().lower().replace("-", "_")


def _connection_nominated_headers(headers: dict[str, str]) -> set[str]:
    nominated: set[str] = set()
    for name, value in headers.items():
        if name.lower() != "connection":
            continue
        nominated.update(
            token.strip().lower()
            for token in value.split(",")
            if token.strip()
        )
    return nominated
