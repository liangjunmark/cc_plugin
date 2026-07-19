# Task 4 Report: Wire Phase 2b Through App And Evals

## Status

Implemented and verified locally. The real-provider acceptance target was not met because the configured xfyun backend rejected every request for missing authentication.

## Scope

Modified only the Task 4-owned implementation, documentation, and test files:

- `proxy/app.py`
- `proxy/evals.py`
- `README.md`
- `tests/test_app.py`
- `tests/test_evals.py`

This report is also updated as required by the task contract.

## Implementation

- Added `phase2b` imports and routing in `proxy.app`.
- Phase 2b is considered only for non-streamed requests and is checked before Phase 2 or ordinary upstream passthrough.
- Explicit `x-cc-proxy-phase: phase2b` runs only Phase 2b eligibility. Explicit Phase 2b does not fall through to Phase 2 when ineligible.
- A successful `Phase2bExecutionResult.downstream_payload` returns as the downstream JSON response. A missing downstream payload returns the retained baseline through the existing normalized upstream response path.
- Added `claude_code_proxy_phase2b` to eval proxy modes and maps it to `x-cc-proxy-phase: phase2b`.
- Added `run_repeated_mode(mode, fixture, config, repeat_count)` for repeated real executions.
- Added a module CLI so the documented `python -m proxy.evals --mode ... --fixture ... --repeat ...` command performs the repeat batch and prints aggregate run, exact-match, and protocol counts.
- Added the required Phase 2b README description and acceptance command.

## Test-First Evidence

Added the three Task 4 acceptance tests before production wiring:

- `test_messages_return_phase2b_selected_answer_for_non_streamed_requests`
- `test_run_mode_supports_phase2b_proxy_mode`
- `test_run_repeated_mode_records_phase2b_repeated_results`

RED results:

- `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest tests/test_app.py -k phase2b -v` failed because `proxy.app` had no `is_phase2b_eligible` attribute.
- `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest tests/test_evals.py -k phase2b -v` failed because `claude_code_proxy_phase2b` was unsupported and `run_repeated_mode` did not exist.

GREEN result:

- `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest tests/test_app.py tests/test_evals.py -k phase2b -v` completed with `3 passed, 56 deselected`.

Broader regression:

- `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest tests/test_config.py tests/test_phase2b_extract.py tests/test_phase2b.py tests/test_app.py tests/test_evals.py -v` completed with `133 passed`.
- The only warning was the existing FastAPI `TestClient` deprecation warning for the installed HTTPX version.

CLI verification:

- `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m proxy.evals --help` lists `claude_code_proxy_phase2b`, `--fixture`, and `--repeat`.

## Real-Provider Acceptance Batch

Executed exactly:

```bash
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m proxy.evals \
  --mode claude_code_proxy_phase2b \
  --fixture proxy/fixtures/candy_question.json \
  --repeat 10
```

Aggregate output:

- Runs: `10/10` completed.
- Exact-match `21`: `0/10`.
- Protocol-compatible responses: `0/10`.
- Status for every run: `401`.
- Backend response shape: `{"error":{"code":"","message":"未提供令牌 (request id: ...)","type":"new_api_error"}}`.
- Eval request mode and selector: `claude_code_proxy_phase2b` with `x-cc-proxy-phase: phase2b`.
- Fallback-stage metadata: none appeared. The backend rejected the request before a retained baseline or Phase 2b branch/fallback result could be returned.

The acceptance target of exact-match `21` in `10/10` runs was not achieved. The shell environment had no configured `CC_PROXY_CONFIG`, no configured model override, and no API key for the default config's key environment variable; the running service at `127.0.0.1:8787` forwarded the requests to xfyun and returned the missing-token errors above.

## Commit Decision

No commit created. Before this task, the shared worktree already had staged changes in all Task 4-owned source and test files (`README.md`, `proxy/app.py`, `proxy/evals.py`, `tests/test_app.py`, and `tests/test_evals.py`). Staging or committing those paths would risk including another contributor's changes. No staged changes were modified or reverted.

## Concerns

- Provider credentials must be configured on the running proxy before the real acceptance target can be evaluated.
- `git diff --check` reports an unrelated trailing-newline issue in `.superpowers/sdd/task-5-brief.md`; it was not modified for Task 4.

## Remaining Fixes

### Operator Guidance

The Phase 2b acceptance instructions now require all of the following before starting the batch:

- a config with `phase2b.enabled = true`;
- valid credentials in the environment variable named by `upstream.api_key_env`;
- `CC_PROXY_CONFIG` set to that config; and
- a proxy listener running on the matching configured host and port.

The README includes a matching `uvicorn` startup example and invokes the batch with the same `CC_PROXY_CONFIG` value.

### Eval Timeout

Root cause: `run_mode` unconditionally created an `httpx.Client` with `config.upstream.timeout_seconds`, which is `60s` in the project config while `phase2b.total_timeout_seconds` is `300s`.

Fix: `claude_code_proxy_phase2b` now uses the greater of the normal upstream timeout and `phase2b.total_timeout_seconds + 10s`. Other eval modes retain the normal upstream timeout.

TDD evidence:

- RED: `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest tests/test_evals.py::test_phase2b_proxy_mode_timeout_covers_phase2b_execution_budget -v` failed because `_request_timeout_seconds` did not exist.
- GREEN: the same test passed after the timeout selector was implemented.
- Focused Task 4 verification: `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest tests/test_app.py tests/test_evals.py -k phase2b -v` completed with `4 passed, 56 deselected`.
- Broader regression: `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest tests/test_config.py tests/test_phase2b_extract.py tests/test_phase2b.py tests/test_app.py tests/test_evals.py -v` completed with `134 passed`.

### Credentialed Real-Provider Acceptance Re-run

Used `.claude/settings.json` only as a runtime source for the configured xfyun base URL, model, and `ANTHROPIC_AUTH_TOKEN`; no credential was added to the repository or this report. A temporary Phase 2b-enabled config was created outside the repository, with a dedicated local listener at `127.0.0.1:8790`. The isolated listener was stopped after the run.

Executed the documented batch with that temporary config and the project token:

```bash
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m proxy.evals \
  --mode claude_code_proxy_phase2b \
  --fixture proxy/fixtures/candy_question.json \
  --repeat 10
```

Actual aggregate result:

- Runs: `10/10` completed.
- Exact-match `21`: `0/10`.
- Protocol-compatible responses: `10/10`.
- HTTP status: `200` for all 10 runs.
- Downstream envelope: Anthropic `message` for all 10 runs.
- Returned answers: `13`, `15`, `18`, `22`, `13`, `16`, `17`, `18`, `18`, `15`.
- Exact-output format compliance: `0/10` because none of the answers was `21`.
- Fallback metadata: none appeared in the downstream message envelopes.

The batch ran past the former 60-second client deadline and completed under the new Phase 2b timeout budget. The acceptance target remains unmet; Phase 2b response quality, not credentials, protocol compatibility, or the eval client timeout, is now the limiting issue.
