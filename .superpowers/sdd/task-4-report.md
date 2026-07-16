# Task 4 Report

## Status

Completed with follow-up fixes and verified locally.

## Implementation

- Added `RetryBudget`, a positive-evidence-only `CapabilityCache`, and `UpstreamResult`.
- Added `UpstreamTransport` with configured HTTPX timeouts, live streamed response preservation, recorder integration, and replay-safe retry handling limited by the configured retry budget and statuses.
- Added the specified stream capability probe payload and status policy, including transport-error fallback to `False`.

## TDD Evidence

1. Added the specified helper tests and ran `.venv/bin/pytest tests/test_upstream.py -v`; it failed because `proxy.upstream` did not exist.
2. Implemented the helpers and transport module; the focused suite passed.
3. During follow-up review, expanded the focused suite to cover retryable statuses, non-replay-safe passthrough, transport exceptions, streaming preservation, and probe transport failures.
4. The expanded suite failed with `TypeError: AsyncClient.send() got an unexpected keyword argument 'timeout'` under `httpx 0.28.1`.
5. Moved the configured timeout into `AsyncClient.build_request` and kept `send(..., stream=...)` for compatibility; the focused suite then passed.

## Verification

- Focused: `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest tests/test_upstream.py -v` -> `8 passed`.
- Full: `PYTHONDONTWRITEBYTECODE=1 .venv/bin/pytest --import-mode=importlib -v` -> `39 passed`.
- Full after follow-up fixes: `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest --import-mode=importlib -v` -> `44 passed`.

## Review

- External task reviewer timed out twice.
- Local task audit confirmed the final transport behavior matches the Task 4 brief and the expanded focused tests cover the previously missed regressions.
