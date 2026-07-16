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

**Task Notes:**
- Keep candy fixture anchored to expected answer `21`.
- Separate answer correctness from exact-output format compliance.
- Keep mode handling explicit so later direct/proxy replay wiring can build on a stable interface.
