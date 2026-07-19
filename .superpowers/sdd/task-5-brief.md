### Task 5: Extend Evaluation And Operator Documentation

**Files:**
- Modify: `proxy/evals.py`
- Modify: `tests/test_evals.py`
- Modify: `README.md`
- Test: `tests/test_evals.py`

**Interfaces:**
- Produces: `SUPPORTED_MODES` including `claude_code_proxy_phase1` and `claude_code_proxy_phase2`
- Produces: `run_mode(mode: str, fixture_path: Path, config: ProxyConfig, ...) -> dict[str, object]`

- [ ] **Step 1: Write the failing evaluation tests**

```python
def test_run_mode_supports_phase2_proxy_mode(config) -> None:
    from proxy.evals import run_mode

    result = run_mode("claude_code_proxy_phase2", Path("proxy/fixtures/candy_question.json"), config, execute=False)
    assert result["mode"] == "claude_code_proxy_phase2"


def test_score_run_keeps_reasoning_and_format_accounting_separate() -> None:
    from proxy.evals import score_run

    score = score_run("21", "答案是 21", exact_output_required=True)
    assert score["correct"] is True
    assert score["format_ok"] is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest tests/test_evals.py::test_run_mode_supports_phase2_proxy_mode tests/test_evals.py::test_score_run_keeps_reasoning_and_format_accounting_separate -v`

Expected: FAIL because phase-2 mode is not defined yet.

- [ ] **Step 3: Write minimal implementation**

```python
PROXY_MODES = {
    "captured_claude_passthrough",
    "captured_claude_rewrite",
    "claude_code_proxy",
    "claude_code_proxy_phase1",
    "claude_code_proxy_phase2",
}


def _target_base_url(mode: str, config: ProxyConfig) -> str:
    if mode in DIRECT_MODES:
        return config.upstream.base_url
    return f"http://{config.server.host}:{config.server.port}"
```

- [ ] **Step 4: Update README operator guidance**

```markdown
## Phase 2

Phase 2 is a non-streamed experimental path layered on top of phase 1.

- `stream=true` requests stay on phase 1.
- Phase 2 keeps one retained phase-1 baseline response for fallback.
- Phase 2 runs two structured candidate solves and at most one adjudication call.
- Compare `claude_code_proxy_phase1` and `claude_code_proxy_phase2` on the same fixtures before claiming improvement.
```

- [ ] **Step 5: Run test to verify it passes**

Run: `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest tests/test_evals.py -v`

Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add proxy/evals.py tests/test_evals.py README.md
git commit -m "feat: add phase2 evaluation modes"
```

