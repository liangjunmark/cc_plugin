import json
import os
from pathlib import Path
from typing import Any

from proxy.config import ProxyConfig
from proxy.schemas import RequestContext

ALWAYS_REDACT_FIELDS = {
    "authorization",
    "proxy_authorization",
    "x_api_key",
    "api_key",
    "cookie",
    "set_cookie",
    "token",
    "secret",
    "password",
    "credential",
}
SECRET_FIELD_SUFFIXES = ("_token", "_secret", "_api_key", "_password", "_credential")
GITHUB_PAT_PREFIXES = ("ghp_", "gho_", "ghu_", "ghs_", "ghr_", "github_pat_")


class Recorder:
    def __init__(self, root: Path, config: ProxyConfig) -> None:
        self.root = root.resolve()
        self.config = config

    def write_artifact(self, context: RequestContext, name: str, payload: dict | str | bytes) -> None:
        log_dir = self._resolve_log_dir(context.log_dir)
        log_dir.mkdir(parents=True, exist_ok=True)
        path = self._resolve_artifact_path(log_dir, name)
        if isinstance(payload, bytes):
            path.write_bytes(payload if self._raw_logging_allowed() else b"<redacted>")
            return
        if isinstance(payload, str):
            path.write_text(payload if self._raw_logging_allowed() else "<redacted>", encoding="utf-8")
            return
        serializable = payload if self._raw_logging_allowed() else _redact_payload(payload, set(self.config.logging.redaction_whitelist))
        path.write_text(json.dumps(serializable, ensure_ascii=False, indent=2), encoding="utf-8")

    def _raw_logging_allowed(self) -> bool:
        if not self.config.logging.allow_raw_payload_logging:
            return False
        return (
            os.environ.get(self.config.logging.unsafe_override_via_env)
            == "YES_I_ACCEPT_RAW_LOGGING_RISK"
        )

    def _resolve_log_dir(self, log_dir: Path) -> Path:
        candidate = log_dir if log_dir.is_absolute() else self.root / log_dir
        resolved = candidate.resolve()
        try:
            resolved.relative_to(self.root)
        except ValueError as exc:
            raise ValueError("artifact path must stay within recorder root") from exc
        return resolved

    def _resolve_artifact_path(self, log_dir: Path, name: str) -> Path:
        if Path(name).name != name or name in {"", ".", ".."}:
            raise ValueError("artifact path must stay within recorder root")
        return log_dir / f"{name}.json"


def _redact_payload(payload: Any, whitelist: set[str]) -> Any:
    if isinstance(payload, dict):
        return {
            key: _redact_value_for_key(key, value, whitelist)
            for key, value in payload.items()
        }
    if isinstance(payload, list):
        return [_redact_payload(item, whitelist) for item in payload]
    if isinstance(payload, str) and _contains_github_pat(payload):
        return "<redacted>"
    return payload


def _redact_value_for_key(key: str, value: Any, whitelist: set[str]) -> Any:
    normalized = key.strip().lower().replace("-", "_")
    if normalized in whitelist and normalized not in ALWAYS_REDACT_FIELDS:
        return _redact_payload(value, whitelist)
    if normalized in ALWAYS_REDACT_FIELDS or normalized.endswith(SECRET_FIELD_SUFFIXES):
        return "<redacted>"
    return _redact_payload(value, whitelist)


def _contains_github_pat(value: str) -> bool:
    lowered = value.lower()
    return any(prefix in lowered for prefix in GITHUB_PAT_PREFIXES)
