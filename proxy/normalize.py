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
SECRET_HEADER_FRAGMENTS = ("authorization", "api-key", "token", "secret")
ALWAYS_REDACT_HEADERS = {
    "authorization",
    "proxy-authorization",
    "x-api-key",
    "api-key",
    "cookie",
    "set-cookie",
}


def filter_forward_headers(headers: dict[str, str], safe_allowlist: set[str]) -> dict[str, str]:
    forwarded: dict[str, str] = {}
    for name, value in headers.items():
        lower = name.lower()
        if lower in HOP_BY_HOP_HEADERS or lower in TRANSPORT_REGENERATED_HEADERS:
            continue
        forwarded[name] = value
    return forwarded


def redact_headers(headers: dict[str, str], safe_allowlist: set[str]) -> dict[str, str]:
    redacted: dict[str, str] = {}
    allow = {name.lower() for name in safe_allowlist}
    for name, value in headers.items():
        lower = name.lower()
        if lower in ALWAYS_REDACT_HEADERS or any(fragment in lower for fragment in SECRET_HEADER_FRAGMENTS):
            redacted[name] = "<redacted>"
        elif lower in allow:
            redacted[name] = value
        else:
            redacted[name] = "<redacted>"
    return redacted


def anthropic_error(status_code: int, message: str, error_type: str = "api_error") -> dict:
    return {"type": "error", "error": {"type": error_type, "message": message}, "status_code": status_code}
