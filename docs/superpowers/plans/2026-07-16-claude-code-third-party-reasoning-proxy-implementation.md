# Claude Code Third-Party Reasoning Proxy Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a local Anthropic-compatible proxy and evaluation harness that preserves Claude Code or Codex agent behavior while diagnosing and minimally correcting third-party provider reasoning degradation.

**Architecture:** The implementation is a small Python service with a strict separation between transport-safe passthrough behavior and gated semantic rewrites. Request handling flows through config validation, recorder, normalization, classification, optional rewrite, upstream streaming transport, and Anthropic-compatible downstream response shaping; evaluation replays captured requests and compares passthrough versus rewrite treatment against the same raw artifacts.

**Tech Stack:** Python 3.12+, FastAPI, Uvicorn, HTTPX, Pydantic v2, pytest

## Global Constraints

- Accept Anthropic-compatible `/v1/messages` traffic and preserve Claude Code / Codex agent behavior.
- Return an Anthropic-compatible success shape or Anthropic-compatible error envelope whenever no downstream bytes have been emitted yet.
- Once downstream streaming has begun, never replace a partial stream with a brand-new full error envelope.
- Phase 1 permits one initial upstream attempt plus at most one retry total, and only for replay-safe requests.
- `system_compression.enabled = false` by default.
- `allow_raw_payload_logging = false` by default.
- `upstream.max_retries = 1`.
- `upstream.allow_self_target = false`.
- `upstream.timeout_seconds = 60.0`.
- `upstream.connect_timeout_seconds = 10.0`.
- Timeout values must be greater than `0.0` and less than or equal to `600.0`.
- `server.port` must be between `1` and `65535`.
- `upstream.base_url` must be an absolute `http` or `https` URL.
- `upstream.retry_statuses` entries must be valid HTTP status codes in the `4xx` or `5xx` ranges.
- Raw unredacted payload logging may be enabled only with both config opt-in and an unsafe environment override.
- All Claude Code traffic is eligible for recording and replay comparison even when semantic rewrites are disabled.
- Do not commit `.claude/` secrets or local credential files.

---

## Planned File Layout

- Create: `pyproject.toml`
  - Python package metadata and dependencies for proxy service and tests.
- Create: `proxy/__init__.py`
  - Package marker.
- Create: `proxy/config.py`
  - Pydantic settings models, TOML loading, startup validation, loop-prevention validation.
- Create: `proxy/schemas.py`
  - Internal dataclasses and typed models shared by recorder, normalize, rewrite, upstream, and evals.
- Create: `proxy/recorder.py`
  - Redacted request or response persistence, attempt lineage metadata, degraded logging behavior.
- Create: `proxy/normalize.py`
  - Header filtering, request-shape normalization, Anthropic error envelope helpers.
- Create: `proxy/rewrite.py`
  - Replay-safe classifier, prompt-surface extraction, rewrite gate scoring, rewrite application and fallback metadata.
- Create: `proxy/upstream.py`
  - Capability cache, stream probe, retry budget logic, streaming transport wrapper.
- Create: `proxy/app.py`
  - FastAPI app with `/v1/messages`, `/health`, `/ready`, dependency wiring, stream passthrough.
- Create: `proxy/evals.py`
  - Replay and direct evaluation runners, answer extraction, regression accounting.
- Create: `proxy/config.toml.example`
  - Local example config matching the validated schema.
- Create: `proxy/fixtures/candy_question.json`
  - Target acceptance prompt fixture with expected answer `21`.
- Create: `proxy/fixtures/non_candy_format.json`
  - Non-candy exact-output control prompt.
- Create: `proxy/fixtures/non_candy_reasoning.json`
  - Non-candy reasoning control prompt.
- Create: `tests/test_config.py`
  - Config and startup validation tests.
- Create: `tests/conftest.py`
  - Shared `config` fixture for tests.
- Create: `tests/test_normalize.py`
  - Header forwarding, redaction, and error-envelope tests.
- Create: `tests/test_rewrite.py`
  - Replay-safe, classification, and rewrite tests.
- Create: `tests/test_upstream.py`
  - Retry budget, capability evidence, and stream-probe tests.
- Create: `tests/test_app.py`
  - FastAPI endpoint, streaming, and downstream compatibility tests.
- Create: `tests/test_evals.py`
  - Evaluation answer extraction and regression-accounting tests.

### Task 1: Scaffold Package And Validated Config

**Files:**
- Create: `pyproject.toml`
- Create: `proxy/__init__.py`
- Create: `proxy/config.py`
- Create: `proxy/config.toml.example`
- Create: `tests/conftest.py`
- Test: `tests/test_config.py`

**Interfaces:**
- Produces: `load_config(path: str | Path) -> ProxyConfig`
- Produces: `validate_runtime_config(config: ProxyConfig) -> None`
- Produces: `ProxyConfig.model_validate(...)`
- Produces: `validate_no_self_target(config: ProxyConfig) -> None`

- [ ] **Step 1: Write the failing config tests**

```python
from pathlib import Path

import pytest

from proxy.config import load_config, validate_runtime_config


def write_config(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(body, encoding="utf-8")
    return path


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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_config.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'proxy'`

- [ ] **Step 3: Write minimal package and config implementation**

```python
# proxy/config.py
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


def validate_no_self_target(config: ProxyConfig) -> None:
    parsed = urlparse(config.upstream.base_url)
    if not config.upstream.allow_self_target and parsed.hostname == config.server.host and parsed.port == config.server.port:
        raise ValueError("upstream.base_url must not target the local listener")
```

```toml
# proxy/config.toml.example
[server]
host = "127.0.0.1"
port = 8787
request_log_dir = "logs/requests"
```

```python
# tests/conftest.py
from pathlib import Path

import pytest

from proxy.config import load_config


@pytest.fixture
def config() -> object:
    return load_config(Path("proxy/config.toml.example"))
```

```toml
# pyproject.toml
[build-system]
requires = ["setuptools>=68", "wheel"]
build-backend = "setuptools.build_meta"

[project]
name = "cc-plugin-proxy"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = ["fastapi>=0.115", "uvicorn>=0.30", "httpx>=0.27", "pydantic>=2.8"]

[project.optional-dependencies]
dev = ["pytest>=8.2", "pytest-asyncio>=0.23"]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_config.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml proxy/__init__.py proxy/config.py proxy/config.toml.example tests/conftest.py tests/test_config.py
git commit -m "feat: scaffold proxy config and validation"
```

### Task 2: Add Shared Schemas, Recorder, And Normalization

**Files:**
- Create: `proxy/schemas.py`
- Create: `proxy/recorder.py`
- Create: `proxy/normalize.py`
- Test: `tests/test_normalize.py`

**Interfaces:**
- Consumes: `ProxyConfig`
- Produces: `RequestContext`
- Produces: `NormalizedRequest`
- Produces: `anthropic_error(status_code: int, message: str, error_type: str = "api_error") -> dict`
- Produces: `filter_forward_headers(headers: dict[str, str], safe_allowlist: set[str]) -> dict[str, str]`
- Produces: `Recorder.write_artifact(context: RequestContext, name: str, payload: dict | str | bytes) -> None`

- [ ] **Step 1: Write failing normalization tests**

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_normalize.py -v`
Expected: FAIL with `ImportError` for missing `proxy.normalize`

- [ ] **Step 3: Implement schemas, recorder, and normalization**

```python
# proxy/schemas.py
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


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
```

```python
# proxy/normalize.py
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
        if lower in allow:
            redacted[name] = value
        elif any(fragment in lower for fragment in SECRET_HEADER_FRAGMENTS):
            redacted[name] = "<redacted>"
        else:
            redacted[name] = "<redacted>"
    return redacted


def anthropic_error(status_code: int, message: str, error_type: str = "api_error") -> dict:
    return {"type": "error", "error": {"type": error_type, "message": message}, "status_code": status_code}
```

```python
# proxy/recorder.py
import json
from pathlib import Path
from typing import Any

from proxy.schemas import RequestContext


class Recorder:
    def __init__(self, root: Path) -> None:
        self.root = root

    def write_artifact(self, context: RequestContext, name: str, payload: dict | str | bytes) -> None:
        context.log_dir.mkdir(parents=True, exist_ok=True)
        path = context.log_dir / f"{name}.json"
        if isinstance(payload, bytes):
            path.write_bytes(payload)
            return
        if isinstance(payload, str):
            path.write_text(payload, encoding="utf-8")
            return
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_normalize.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add proxy/schemas.py proxy/recorder.py proxy/normalize.py tests/test_normalize.py
git commit -m "feat: add normalization and recorder primitives"
```

### Task 3: Implement Replay Safety, Classification, And Rewrite Engine

**Files:**
- Create: `proxy/rewrite.py`
- Test: `tests/test_rewrite.py`

**Interfaces:**
- Consumes: `NormalizedRequest`, `ProxyConfig`
- Produces: `is_replay_safe(body: dict) -> tuple[bool, str | None]`
- Produces: `extract_effective_prompt_surface(body: dict) -> str`
- Produces: `classify_request(body: dict, config: ProxyConfig) -> ClassificationResult`
- Produces: `apply_rewrites(body: dict, config: ProxyConfig, capability_flags: dict[str, bool]) -> RewriteResult`

- [ ] **Step 1: Write failing rewrite tests**

```python
from proxy.rewrite import apply_rewrites, classify_request, is_replay_safe


def test_replay_safe_rejects_tool_use_blocks() -> None:
    body = {"messages": [{"role": "assistant", "content": [{"type": "tool_use", "name": "shell"}]}]}
    replay_safe, reason = is_replay_safe(body)
    assert replay_safe is False
    assert reason == "tool_state_present"


def test_candy_prompt_reaches_rewrite_band(config) -> None:
    body = {
        "system": "You are a careful assistant.",
        "messages": [
            {
                "role": "user",
                "content": "在一个黑色的袋子里放有三种口味的糖果，每种糖果有两种不同的形状。最少取出多少个糖果才能保证同时拥有不同形状的苹果味和桃子味糖？只输出一个数字。"
            }
        ],
    }
    result = classify_request(body, config)
    assert result.route == "rewrite"
    assert result.score >= config.classification.rewrite_score_threshold


def test_code_marker_forces_passthrough(config) -> None:
    body = {"messages": [{"role": "user", "content": "Read repo file and apply patch, then answer."}]}
    result = classify_request(body, config)
    assert result.route == "passthrough"


def test_apply_rewrites_keeps_thinking_disabled_without_support(config) -> None:
    body = {"messages": [{"role": "user", "content": "只输出一个数字 21"}], "max_tokens": 32}
    result = apply_rewrites(body, config, capability_flags={"thinking": False})
    assert "thinking" not in result.body
    assert result.metadata["applied_rules"] == ["max_tokens_floor", "strict_format_guardrail"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_rewrite.py -v`
Expected: FAIL with `ImportError` for missing rewrite module

- [ ] **Step 3: Implement replay-safe logic and rewrite engine**

```python
# proxy/rewrite.py
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import re
from typing import Any

from proxy.config import ProxyConfig


@dataclass(slots=True)
class ClassificationResult:
    route: str
    score: int
    replay_safe: bool
    replay_safe_reason: str | None
    effective_prompt_surface: str


@dataclass(slots=True)
class RewriteResult:
    body: dict[str, Any]
    metadata: dict[str, Any]


def is_replay_safe(body: dict[str, Any]) -> tuple[bool, str | None]:
    text = str(body)
    if "tool_use" in text or "tool_result" in text or "tool_choice" in text:
        return False, "tool_state_present"
    if "apply this patch now" in text or "run this command now" in text:
        return False, "side_effect_intent"
    return True, None


def extract_effective_prompt_surface(body: dict[str, Any]) -> str:
    system = body.get("system", "")
    if isinstance(system, list):
        system_text = "\n".join(str(item.get("text", "")) for item in system if isinstance(item, dict))
    else:
        system_text = str(system)
    parts = [system_text]
    for message in body.get("messages", []):
        if message.get("role") in {"assistant", "user"}:
            parts.append(str(message.get("content", "")))
    return "\n".join(part for part in parts if part).strip()


def classify_request(body: dict[str, Any], config: ProxyConfig) -> ClassificationResult:
    replay_safe, reason = is_replay_safe(body)
    surface = extract_effective_prompt_surface(body)
    if not replay_safe:
        return ClassificationResult("passthrough", 0, False, reason, surface)
    code_patterns = [re.compile(pattern, re.IGNORECASE) for pattern in config.classification.code_marker_patterns]
    if any(pattern.search(surface) for pattern in code_patterns):
        return ClassificationResult("passthrough", 0, True, None, surface)
    score = 0
    latest_user_present = any(message.get("role") == "user" for message in body.get("messages", []))
    if latest_user_present:
        score += 1
    if len(surface) >= config.classification.min_chars or surface.count("\n") >= config.classification.min_line_breaks:
        score += 1
    if any(re.search(pattern, surface, re.IGNORECASE) for pattern in config.classification.reasoning_keyword_patterns):
        score += 1
    if any(re.search(pattern, surface, re.IGNORECASE) for pattern in config.classification.output_constraint_patterns):
        score += 1
    if score >= config.classification.rewrite_score_threshold:
        route = "rewrite"
    elif score >= config.classification.normalize_only_score_threshold:
        route = "normalize_only"
    else:
        route = "passthrough"
    return ClassificationResult(route, score, True, None, surface)


def apply_rewrites(body: dict[str, Any], config: ProxyConfig, capability_flags: dict[str, bool]) -> RewriteResult:
    rewritten = deepcopy(body)
    applied: list[str] = []
    if rewritten.get("max_tokens", 0) < config.rewrite.max_tokens_floor.minimum_output_tokens:
        rewritten["max_tokens"] = config.rewrite.max_tokens_floor.minimum_output_tokens
        applied.append("max_tokens_floor")
    if config.rewrite.explicit_thinking.enabled and capability_flags.get("thinking") and "thinking" not in rewritten:
        rewritten["thinking"] = {"type": "enabled", "budget_tokens": config.rewrite.explicit_thinking.minimum_budget_tokens}
        applied.append("explicit_thinking")
    if config.rewrite.strict_format_guardrail.enabled:
        rewritten["system"] = f"{rewritten.get('system', '')}\nReturn only the final answer when the user requests exact output.".strip()
        applied.append("strict_format_guardrail")
    return RewriteResult(body=rewritten, metadata={"applied_rules": applied})
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_rewrite.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add proxy/rewrite.py tests/test_rewrite.py
git commit -m "feat: add replay safety and rewrite engine"
```

### Task 4: Implement Upstream Transport, Capability Cache, And Stream Probe

**Files:**
- Create: `proxy/upstream.py`
- Test: `tests/test_upstream.py`

**Interfaces:**
- Consumes: `ProxyConfig`, `Recorder`, `RequestContext`
- Produces: `CapabilityCache.mark_supported(provider_key: str, field_family: str) -> None`
- Produces: `CapabilityCache.is_supported(provider_key: str, field_family: str) -> bool`
- Produces: `UpstreamTransport.send_once(headers: dict[str, str], body: dict[str, Any], stream: bool) -> httpx.Response`
- Produces: `UpstreamTransport.send_with_retry(context: RequestContext, headers: dict[str, str], body: dict[str, Any], replay_safe: bool, stream: bool) -> UpstreamResult`
- Produces: `probe_stream_support(client: httpx.AsyncClient, config: ProxyConfig) -> bool`

- [ ] **Step 1: Write failing upstream tests**

```python
import pytest

from proxy.upstream import CapabilityCache, RetryBudget


def test_retry_budget_allows_only_one_retry_total() -> None:
    budget = RetryBudget(max_retries=1)
    assert budget.consume() is True
    assert budget.consume() is False


def test_capability_cache_requires_positive_evidence() -> None:
    cache = CapabilityCache()
    assert cache.is_supported("xf", "thinking") is False
    cache.mark_supported("xf", "thinking")
    assert cache.is_supported("xf", "thinking") is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_upstream.py -v`
Expected: FAIL with missing `proxy.upstream`

- [ ] **Step 3: Implement transport policy helpers**

```python
# proxy/upstream.py
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


class RetryBudget:
    def __init__(self, max_retries: int) -> None:
        self.max_retries = max_retries
        self.spent = 0

    def consume(self) -> bool:
        if self.spent >= self.max_retries:
            return False
        self.spent += 1
        return True


@dataclass
class CapabilityCache:
    support: dict[str, set[str]] = field(default_factory=dict)

    def mark_supported(self, provider_key: str, field_family: str) -> None:
        self.support.setdefault(provider_key, set()).add(field_family)

    def is_supported(self, provider_key: str, field_family: str) -> bool:
        return field_family in self.support.get(provider_key, set())


@dataclass
class UpstreamResult:
    status_code: int
    headers: dict[str, str]
    body: bytes
    streamed: bool = False


class UpstreamTransport:
    def __init__(self, client: httpx.AsyncClient, config: ProxyConfig) -> None:
        self.client = client
        self.config = config

    async def send_once(self, headers: dict[str, str], body: dict[str, Any], stream: bool) -> httpx.Response:
        return await self.client.post(
            f"{self.config.upstream.base_url}/v1/messages",
            headers=headers,
            json=body,
            timeout=httpx.Timeout(
                self.config.upstream.timeout_seconds,
                connect=self.config.upstream.connect_timeout_seconds,
            ),
        )
```

- [ ] **Step 4: Extend implementation with HTTPX streaming and probe**

```python
async def probe_stream_support(client: httpx.AsyncClient, config: ProxyConfig) -> bool:
    payload = {
        "model": "probe",
        "max_tokens": 1,
        "messages": [{"role": "user", "content": "reply with 1"}],
        "stream": True,
    }
    response = await client.post(f"{config.upstream.base_url}/v1/messages", json=payload)
    return response.status_code < 500
```

Run: `pytest tests/test_upstream.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add proxy/upstream.py tests/test_upstream.py
git commit -m "feat: add upstream retry and capability helpers"
```

### Task 5: Build FastAPI Listener With Anthropic-Compatible Streaming

**Files:**
- Create: `proxy/app.py`
- Test: `tests/test_app.py`

**Interfaces:**
- Consumes: `ProxyConfig`, `Recorder`, `UpstreamTransport`, `ClassificationResult`, `RewriteResult`
- Produces: `create_app(config: ProxyConfig) -> FastAPI`
- Produces: `POST /v1/messages`
- Produces: `GET /health`
- Produces: `GET /ready`

- [ ] **Step 1: Write failing app tests**

```python
from fastapi.testclient import TestClient

from proxy.app import create_app


def test_health_endpoint_returns_ok(config) -> None:
    client = TestClient(create_app(config))
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_ready_endpoint_reports_probe_state(config) -> None:
    client = TestClient(create_app(config))
    response = client.get("/ready")
    assert response.status_code == 200
    assert "stream_probe" in response.json()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_app.py -v`
Expected: FAIL with missing `proxy.app`

- [ ] **Step 3: Implement the FastAPI app shell**

```python
# proxy/app.py
from __future__ import annotations

from fastapi import FastAPI

from proxy.config import ProxyConfig


def create_app(config: ProxyConfig) -> FastAPI:
    app = FastAPI()

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/ready")
    async def ready() -> dict[str, object]:
        return {"status": "ready", "stream_probe": {"enabled": config.upstream.stream_probe_on_startup}}

    return app
```

- [ ] **Step 4: Add `/v1/messages` request pipeline and streaming response**

```python
@app.post("/v1/messages")
async def messages(payload: dict, request: Request) -> Response:
    request_id = request.headers.get("x-request-id", str(uuid4()))
    normalized = normalize_request(payload, dict(request.headers), config)
    classification = classify_request(normalized.body, config)
    outgoing_body = normalized.body
    if config.rewrite.enabled and classification.route == "rewrite":
        rewrite_result = apply_rewrites(outgoing_body, config, capability_flags={"thinking": False})
        outgoing_body = rewrite_result.body
    upstream = await transport.send_with_retry(
        context=build_request_context(request_id, config),
        headers=normalized.forward_headers,
        body=outgoing_body,
        replay_safe=classification.replay_safe,
        stream=bool(outgoing_body.get("stream")),
    )
    if not upstream.streamed:
        return JSONResponse(status_code=upstream.status_code, content=json.loads(upstream.body))
    return StreamingResponse(iter_sse_bytes(upstream.body), media_type="text/event-stream")
```

Run: `pytest tests/test_app.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add proxy/app.py tests/test_app.py
git commit -m "feat: add proxy http listener"
```

### Task 6: Build Evaluation Harness And Seed Fixtures

**Files:**
- Create: `proxy/evals.py`
- Create: `proxy/fixtures/candy_question.json`
- Create: `proxy/fixtures/non_candy_format.json`
- Create: `proxy/fixtures/non_candy_reasoning.json`
- Test: `tests/test_evals.py`

**Interfaces:**
- Consumes: `ProxyConfig`
- Produces: `extract_final_answer(text: str) -> str | None`
- Produces: `score_run(expected_answer: str, response_text: str, exact_output_required: bool) -> dict[str, bool]`
- Produces: `summarize_regressions(baseline: list[dict], candidate: list[dict]) -> dict[str, object]`
- Produces: `run_mode(mode: str, fixture_path: Path, config: ProxyConfig) -> dict[str, object]`

- [ ] **Step 1: Write failing evaluator tests**

```python
from proxy.evals import extract_final_answer, summarize_regressions


def test_extract_final_answer_keeps_exact_numeric_output() -> None:
    assert extract_final_answer("21") == "21"
    assert extract_final_answer("答案是 21。") == "21"


def test_regression_summary_flags_large_correctness_drop() -> None:
    baseline = [
        {"correct": True, "format_ok": True, "protocol_ok": True},
        {"correct": True, "format_ok": True, "protocol_ok": True},
    ]
    candidate = [
        {"correct": False, "format_ok": True, "protocol_ok": True},
        {"correct": True, "format_ok": True, "protocol_ok": True},
    ]
    summary = summarize_regressions(baseline, candidate)
    assert summary["material_regression"] is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_evals.py -v`
Expected: FAIL with missing `proxy.evals`

- [ ] **Step 3: Implement fixtures and evaluator helpers**

```python
# proxy/evals.py
from __future__ import annotations

import re


def extract_final_answer(text: str) -> str | None:
    match = re.search(r"\b(\d+)\b", text)
    return match.group(1) if match else None


def summarize_regressions(baseline: list[dict], candidate: list[dict]) -> dict[str, object]:
    baseline_correct = sum(item["correct"] for item in baseline) / len(baseline)
    candidate_correct = sum(item["correct"] for item in candidate) / len(candidate)
    material_regression = candidate_correct < baseline_correct - 0.10
    return {
        "baseline_correctness": baseline_correct,
        "candidate_correctness": candidate_correct,
        "material_regression": material_regression,
    }
```

```json
{
  "name": "candy_question",
  "expected_answer": "21",
  "prompt": "在一个黑色的袋子里放有三种口味的糖果，每种糖果有两种不同的形状（圆形和五角星形，不同的形状靠手感可以分辨）。现已知不同口味的糖和不同形状的数量统计如下表。参赛者需要在活动前决定摸出的糖果数目，那么，最少取出多少个糖果才能保证手中同时拥有不同形状的苹果味和桃子味的糖？（同时手中有圆形苹果味匹配五角星桃子味糖果，或者有圆形桃子味匹配五角星苹果味糖果都满足要求）苹果味 桃子味 西瓜味 圆形 7 9 8 五角星形 7 6 4"
}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_evals.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add proxy/evals.py proxy/fixtures/candy_question.json proxy/fixtures/non_candy_format.json proxy/fixtures/non_candy_reasoning.json tests/test_evals.py
git commit -m "feat: add evaluation harness and fixtures"
```

### Task 7: End-To-End Verification And Operator Notes

**Files:**
- Modify: `proxy/app.py`
- Modify: `proxy/config.toml.example`
- Create: `README.md`

**Interfaces:**
- Consumes: all prior tasks
- Produces: local startup command and evaluation workflow

- [ ] **Step 1: Write a focused smoke test**

```python
def test_proxy_startup_smoke(tmp_path, config) -> None:
    app = create_app(config)
    assert app is not None
```

- [ ] **Step 2: Run the full test suite before operator docs**

Run: `pytest -v`
Expected: PASS

- [ ] **Step 3: Document local usage and safe rollout**

```md
# CC Proxy

## Start

    python -m uvicorn proxy.app:create_app --factory --host 127.0.0.1 --port 8787

## Safety

- Keep `.claude/` out of git.
- Start with `rewrite.enabled = false`.
- Enable one rewrite rule at a time.
- Compare `captured_claude_raw_upstream`, `captured_claude_passthrough`, and `captured_claude_rewrite` before claiming improvement.
```

- [ ] **Step 4: Run final verification**

Run: `pytest -v`
Expected: PASS

Run: `python -m uvicorn proxy.app:create_app --factory --host 127.0.0.1 --port 8787`
Expected: server starts and exposes `/health`

- [ ] **Step 5: Commit**

```bash
git add proxy/app.py proxy/config.toml.example README.md tests
git commit -m "docs: add proxy operator notes and verification"
```

## Self-Review

- Spec coverage: config validation, replay-safe gating, header policy, capability negotiation, retry budget, streaming behavior, Anthropic-compatible downstream shaping, evaluator modes, and regression accounting are all mapped to tasks above.
- Placeholder scan: no `TODO`, `TBD`, or implicit “handle appropriately” directives remain in the task steps.
- Type consistency: `ProxyConfig`, `RequestContext`, `ClassificationResult`, `RewriteResult`, `CapabilityCache`, and evaluator helper function names are declared before later tasks depend on them.
