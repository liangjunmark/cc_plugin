# Task 5 Report

## Status

DONE

## Files Changed

- `proxy/evals.py`
- `tests/test_evals.py`
- `README.md`
- `.superpowers/sdd/task-5-report.md`

## Implementation Summary

- Added `claude_code_proxy_phase1` and `claude_code_proxy_phase2` as supported proxy evaluation modes.
- Preserved direct-mode upstream routing and proxy-mode local routing.
- Documented Phase 2's non-streamed behavior, fallback baseline, candidate/adjudication limits, and required Phase 1 versus Phase 2 comparison.

## Test Commands And Results

- Red: `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest tests/test_evals.py::test_run_mode_supports_phase2_proxy_mode tests/test_evals.py::test_score_run_keeps_reasoning_and_format_accounting_separate -v` -> 1 failed, 1 passed. The phase-2 mode was unsupported as expected.
- Green: `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest tests/test_evals.py -v` -> 11 passed.

## Concerns

- Evaluation-mode support only selects the local proxy target. Phase 2 request execution remains owned by the proxy implementation.

## Review Fixes

- Added `x-cc-proxy-phase` request-header selection in the eval harness for `claude_code_proxy_phase1` and `claude_code_proxy_phase2`.
- Updated the proxy app to honor the selector while preserving the reviewed defaults:
  - `phase1` forces the ordinary path.
  - `phase2` enables the phase-2 path for eligible non-streamed requests.
  - no selector preserves the existing auto-routing behavior for normal proxy traffic.
- Added eval and app coverage for selector emission, explicit phase1 bypass, explicit phase2 routing, and preserved auto-routing.

## Review Fix Verification

- RED: focused selector tests reported the intended failures before implementation.
- GREEN: focused selector tests passed: 5 passed.
- Regression: `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest tests/test_evals.py tests/test_app.py -v` passed: 27 passed.

## Final Local Correction

- Restored the default proxy selector behavior to `auto` so ordinary eligible requests still take the phase-2 path when no explicit selector header is present.
- Kept explicit `phase1` bypass and explicit `phase2` enablement intact for evaluation-mode comparisons.
- Verification: `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest tests/test_app.py::test_messages_auto_route_still_uses_phase2_for_eligible_non_streamed_requests -v` passed, and `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest tests/test_evals.py tests/test_app.py -v` passed: 27 passed.

## Review Fix: Proxy Phase Selector

### Implementation

- `claude_code_proxy_phase1` now emits `x-cc-proxy-phase: phase1` and `claude_code_proxy_phase2` emits `x-cc-proxy-phase: phase2`.
- The proxy consumes the selector, strips it before upstream forwarding, and invokes Phase 2 only for an explicitly selected non-streamed request that remains otherwise eligible.
- Explicit Phase 1 and unselected traffic retain the baseline path. The existing streamed guard and raw baseline fallback remain unchanged.

### Test Commands And Results

- Red: `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest tests/test_evals.py::test_run_mode_sets_proxy_phase_selector tests/test_app.py::test_messages_keep_phase1_selector_on_baseline_path -v` -> 3 failed as intended: both selector headers were missing, and the Phase 1 selector incorrectly returned the mocked Phase 2 result.
- Focused green: `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest tests/test_evals.py::test_run_mode_sets_proxy_phase_selector tests/test_app.py::test_messages_keep_phase1_selector_on_baseline_path tests/test_app.py::test_messages_bypass_phase2_for_streamed_requests tests/test_app.py::test_messages_return_phase2_selected_answer_for_non_streamed_requests -v` -> 5 passed.
- Full touched suites: `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest tests/test_evals.py tests/test_app.py -v` -> 26 passed.

### Comparison Note

- No live quality comparison was run as part of this routing fix; no Phase 2 improvement claim is made. Operators must compare `claude_code_proxy_phase1` and `claude_code_proxy_phase2` on the same fixtures before making one.
