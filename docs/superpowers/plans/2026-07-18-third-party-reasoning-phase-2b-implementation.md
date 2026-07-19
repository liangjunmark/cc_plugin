# Third-Party Reasoning Phase 2b Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a narrow, high-cost `phase2b` orchestration path that keeps Claude Code or Codex as the agent while attempting same-provider self-correction for hard single-turn exact-output reasoning prompts.

**Architecture:** Extend the current proxy instead of adding a second service. `phase2b` sits behind the existing classification path, keeps one retained `phase1` baseline for fallback, generates multiple branch drafts, attacks each branch for premise and worst-case errors, and only then compresses one surviving branch into the final exact-output Anthropic-compatible response.

**Tech Stack:** Python 3.12+, FastAPI, Uvicorn, HTTPX, Pydantic v2, pytest

## Global Constraints

- Preserve Claude Code / Codex as the real agent and keep Anthropic-compatible `/v1/messages` downstream behavior.
- `phase2b` applies only to single-turn, non-tool, non-streamed, answer-only or exact-output reasoning requests.
- `phase2b` must not trigger for coding, file-edit, shell, patch, multi-turn, or structured-user-output flows.
- The retained `phase1` baseline response is the only guaranteed fallback source.
- Middle rounds may use tolerant extraction; the final downstream response must remain Anthropic-compatible and exact-output-compatible.
- `phase2b` must never leak branch text, attack text, or ledger text downstream.
- The first implementation optimizes for proving efficacy, not cost; high call counts are acceptable.
- The first implementation fixes `max_branch_count = 3` and uses three distinct branch families: `premise_first`, `quota_first`, and `counterexample_first`.
- Primary acceptance target: the candy-shape fixture must return exact-match `21` in `10/10` repeated runs against the configured xfyun backend.
- Middle-round extraction failures should not automatically discard a branch if semantically useful information can still be salvaged.
- No inbound streaming support for `phase2b`.
- Do not commit `.claude/` secrets or local credential files.

---

## Planned File Layout

- Modify: `proxy/config.py`
  - Add `Phase2bConfig` and validation rules that keep the feature narrow, opt-in, and locally bounded.
- Modify: `proxy/config.toml.example`
  - Add disabled-by-default `phase2b` example settings.
- Modify: `proxy/schemas.py`
  - Add typed dataclasses for raw artifacts, extracted records, branch decisions, and phase2b execution results.
- Create: `proxy/phase2b_extract.py`
  - Implement tolerant parsing and salvage extraction for branch, audit, and attacker replies.
- Create: `proxy/phase2b.py`
  - Implement eligibility checks, prompt builders, orchestration state machine, survivor selection, and final compression.
- Modify: `proxy/app.py`
  - Route `phase2b` after rewrite classification and before ordinary upstream passthrough for eligible non-streamed requests.
- Modify: `proxy/evals.py`
  - Add `claude_code_proxy_phase2b` mode plus per-run metadata for fallback stage and protocol scoring.
- Modify: `README.md`
  - Document `phase2b` scope, cost expectations, and candy-targeted evaluation flow.
- Modify: `tests/test_config.py`
  - Cover config validity and invalid phase2b combinations.
- Create: `tests/test_phase2b_extract.py`
  - Cover strict, salvage, and semantic-fallback extraction paths.
- Create: `tests/test_phase2b.py`
  - Cover eligibility, branch filtering, fallback, survivor flow, and final compression behavior.
- Modify: `tests/test_app.py`
  - Verify app routing to `phase2b`, bypass on streamed/tool/coding requests, and baseline fallback behavior.
- Modify: `tests/test_evals.py`
  - Cover `claude_code_proxy_phase2b` mode and acceptance-fixture scoring.

### Task 1: Add Phase 2b Config And Shared Schemas

**Files:**
- Modify: `proxy/config.py`
- Modify: `proxy/config.toml.example`
- Modify: `proxy/schemas.py`
- Modify: `tests/test_config.py`
- Test: `tests/test_config.py`

**Interfaces:**
- Produces: `class Phase2bConfig(BaseModel)`
- Produces: `validate_phase2b_config(config: ProxyConfig) -> None`
- Produces: `class Phase2bRawArtifact`
- Produces: `class Phase2bExtractedRecord`
- Produces: `class Phase2bBranchDecision`
- Produces: `class Phase2bExecutionResult`

- [ ] **Step 1: Write the failing config and schema tests**

```python
def test_phase2b_defaults_are_valid(tmp_path: Path) -> None:
    from proxy.config import load_config, validate_runtime_config

    path = tmp_path / "config.toml"
    path.write_text(
        '''
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
        stream_probe_on_startup = false
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

        [rewrite.premise_binding_guardrail]
        enabled = true

        [classification]
        enabled = true
        min_chars = 120
        min_line_breaks = 2
        reasoning_keyword_patterns = ["最少", "minimum"]
        output_constraint_patterns = ["只输出", "only output"]
        premise_control_patterns = ["手感", "distinguishable"]
        code_marker_patterns = ["repo", "shell", "patch"]
        rewrite_score_threshold = 4
        normalize_only_score_threshold = 2

        [phase2]
        enabled = false
        trigger_on_routes = ["rewrite"]
        sample_count = 2
        max_adjudication_calls = 1
        max_parallelism = 2
        total_timeout_seconds = 180.0
        require_json_candidates = true
        candidate_roles = ["constraint_reasoner", "counterexample_reasoner"]
        max_candidate_output_tokens = 512
        max_total_upstream_calls = 4
        fallback_to_phase1_on_failure = true
        cost_budget_multiplier = 2.0
        allow_streaming_requests = false

        [phase2b]
        enabled = false
        trigger_on_routes = ["rewrite"]
        max_branch_count = 3
        branch_families = ["premise_first", "quota_first", "counterexample_first"]
        enable_assumption_audit = true
        enable_worst_case_attack = true
        enable_ledger_checks = true
        max_total_upstream_calls = 13
        total_timeout_seconds = 300.0
        allow_tiebreak_round = true
        require_exact_output_requests = true
        ''',
        encoding="utf-8",
    )

    config = load_config(path)
    validate_runtime_config(config)
    assert config.phase2b.max_branch_count == 3
    assert config.phase2b.require_exact_output_requests is True


def test_phase2b_rejects_budget_below_enabled_stage_floor(tmp_path: Path) -> None:
    from proxy.config import load_config, validate_runtime_config

    path = tmp_path / "config.toml"
    path.write_text(VALID_CONFIG_TEXT.replace("max_total_upstream_calls = 13", "max_total_upstream_calls = 11"), encoding="utf-8")

    config = load_config(path)
    with pytest.raises(ValueError, match="phase2b.max_total_upstream_calls"):
        validate_runtime_config(config)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest tests/test_config.py -k phase2b -v`

Expected: FAIL with missing `phase2b` config fields and validation logic.

- [ ] **Step 3: Add `Phase2bConfig` and validation**

```python
class Phase2bConfig(BaseModel):
    enabled: bool = False
    trigger_on_routes: list[str]
    max_branch_count: int = Field(ge=2, le=5)
    branch_families: list[str]
    enable_assumption_audit: bool = True
    enable_worst_case_attack: bool = True
    enable_ledger_checks: bool = True
    max_total_upstream_calls: int = Field(ge=1, le=20)
    total_timeout_seconds: float = Field(gt=0.0, le=900.0)
    allow_tiebreak_round: bool = True
    require_exact_output_requests: bool = True


def validate_phase2b_config(config: ProxyConfig) -> None:
    phase2b = config.phase2b
    if len(phase2b.branch_families) != phase2b.max_branch_count:
        raise ValueError("phase2b.branch_families length must equal phase2b.max_branch_count")
    minimum_calls = 1 + phase2b.max_branch_count
    if phase2b.enable_assumption_audit:
        minimum_calls += phase2b.max_branch_count
    if phase2b.enable_worst_case_attack:
        minimum_calls += phase2b.max_branch_count
    if phase2b.allow_tiebreak_round:
        minimum_calls += 1
    minimum_calls += 1  # final compressor
    if phase2b.max_total_upstream_calls < minimum_calls:
        raise ValueError("phase2b.max_total_upstream_calls must cover baseline, enabled attack stages, and final compression")
```

- [ ] **Step 4: Add shared phase2b dataclasses**

```python
@dataclass(slots=True)
class Phase2bRawArtifact:
    stage: str
    role: str
    raw_text: str
    status_code: int
    observed_answer: str | None
    parse_quality: str


@dataclass(slots=True)
class Phase2bExtractedRecord:
    candidate_answer: str | None
    target_interpretation: str | None
    worst_case_claim: str | None
    controllable_premises: list[str]
    survives: bool | None
    fatal_error_tags: list[str]
    ledger_conflicts: list[str]
    revised_answer_if_any: str | None
    missing_fields: list[str]


@dataclass(slots=True)
class Phase2bBranchDecision:
    branch_id: str
    survived: bool
    fatal_error_tags: list[str]
    chosen_answer: str | None
    reason: str


@dataclass(slots=True)
class Phase2bExecutionResult:
    mode: str
    baseline_upstream: UpstreamResult
    branch_decisions: list[Phase2bBranchDecision]
    downstream_payload: dict[str, Any] | None
    fallback_reason: str | None
```

- [ ] **Step 5: Add example config block**

```toml
[phase2b]
enabled = false
trigger_on_routes = ["rewrite"]
max_branch_count = 3
branch_families = ["premise_first", "quota_first", "counterexample_first"]
enable_assumption_audit = true
enable_worst_case_attack = true
enable_ledger_checks = true
max_total_upstream_calls = 13
total_timeout_seconds = 300.0
allow_tiebreak_round = true
require_exact_output_requests = true
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest tests/test_config.py -k phase2b -v`

Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add proxy/config.py proxy/config.toml.example proxy/schemas.py tests/test_config.py
git commit -m "feat: add phase2b config and schemas"
```

### Task 2: Build Tolerant Phase 2b Extraction

**Files:**
- Create: `proxy/phase2b_extract.py`
- Create: `tests/test_phase2b_extract.py`
- Test: `tests/test_phase2b_extract.py`

**Interfaces:**
- Consumes: `Phase2bRawArtifact`, `Phase2bExtractedRecord`
- Produces: `extract_branch_record(raw_text: str) -> Phase2bExtractedRecord`
- Produces: `extract_audit_record(raw_text: str) -> Phase2bExtractedRecord`
- Produces: `extract_attack_record(raw_text: str) -> Phase2bExtractedRecord`
- Produces: `extract_numeric_answer(text: str) -> str | None`
- Produces: `strip_json_fence(text: str) -> str`

- [ ] **Step 1: Write failing extraction tests**

```python
def test_extract_branch_record_accepts_fenced_json() -> None:
    from proxy.phase2b_extract import extract_branch_record

    record = extract_branch_record(
        '```json\\n{"candidate_answer":"25","target_interpretation":"guarantee both flavors","worst_case_claim":"take all round apple peach and watermelon","controllable_premises":["shape is controllable"]}\\n```'
    )

    assert record.candidate_answer == "25"
    assert record.target_interpretation == "guarantee both flavors"
    assert "shape is controllable" in record.controllable_premises


def test_extract_audit_record_salvages_tagged_text() -> None:
    from proxy.phase2b_extract import extract_audit_record

    record = extract_audit_record(
        "RESULT: fail\\nFATAL: treated controllable shape as random draw\\nREVISED_ANSWER: 21"
    )

    assert record.survives is False
    assert "controllable_shape_as_random" in record.fatal_error_tags
    assert record.revised_answer_if_any == "21"


def test_extract_attack_record_tracks_ledger_conflicts_and_worst_case_tags() -> None:
    from proxy.phase2b_extract import extract_attack_record

    record = extract_attack_record(
        "RESULT: fail\\nFATAL: worst-case construction illegal\\nLEDGER_CONFLICT: shape controllable by touch\\nREVISED_ANSWER: 21"
    )

    assert record.survives is False
    assert "invalid_worst_case_construction" in record.fatal_error_tags
    assert "shape_controllable_by_touch" in record.ledger_conflicts


def test_extract_branch_record_falls_back_to_numeric_answer_and_keywords() -> None:
    from proxy.phase2b_extract import extract_branch_record

    record = extract_branch_record("最坏情况里把形状当成可控条件，再构造反例，答案 21。")

    assert record.candidate_answer == "21"
    assert "可控" in "".join(record.controllable_premises)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest tests/test_phase2b_extract.py -v`

Expected: FAIL with missing `proxy.phase2b_extract`.

- [ ] **Step 3: Implement tolerant extraction helpers**

```python
def strip_json_fence(text: str) -> str:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        lines = cleaned.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        cleaned = "\n".join(lines).strip()
    return cleaned


def extract_numeric_answer(text: str) -> str | None:
    matches = re.findall(r"\b(\d+)\b", text)
    return matches[-1] if matches else None


def extract_branch_record(raw_text: str) -> Phase2bExtractedRecord:
    cleaned = strip_json_fence(raw_text)
    try:
        payload = json.loads(cleaned)
    except json.JSONDecodeError:
        return Phase2bExtractedRecord(
            candidate_answer=extract_numeric_answer(cleaned),
            target_interpretation=cleaned if "guarantee" in cleaned.lower() or "保证" in cleaned else None,
            worst_case_claim=cleaned if "worst" in cleaned.lower() or "最坏" in cleaned else None,
            controllable_premises=[segment for segment in ("shape is controllable", "形状可控", "手感可区分") if segment in cleaned],
            survives=None,
            fatal_error_tags=[],
            ledger_conflicts=[],
            revised_answer_if_any=None,
            missing_fields=[],
        )
    return Phase2bExtractedRecord(
        candidate_answer=str(payload.get("candidate_answer")) if payload.get("candidate_answer") is not None else None,
        target_interpretation=str(payload.get("target_interpretation")) if payload.get("target_interpretation") is not None else None,
        worst_case_claim=str(payload.get("worst_case_claim")) if payload.get("worst_case_claim") is not None else None,
        controllable_premises=[str(item) for item in payload.get("controllable_premises", []) if isinstance(item, str)],
        survives=None,
        fatal_error_tags=[],
        ledger_conflicts=[],
        revised_answer_if_any=None,
        missing_fields=[],
    )
```

- [ ] **Step 4: Implement audit and attack extraction**

```python
def _normalize_fatal_tags(text: str) -> list[str]:
    lowered = text.lower()
    tags: list[str] = []
    if "controllable" in lowered or "可控" in text:
        tags.append("controllable_shape_as_random")
    if "existence" in lowered or "存在" in text:
        tags.append("guarantee_to_existence_swap")
    if "target" in lowered or "目标" in text:
        tags.append("target_condition_misread")
    return tags


def _normalize_attack_tags(text: str) -> tuple[list[str], list[str]]:
    lowered = text.lower()
    fatal_tags: list[str] = []
    ledger_conflicts: list[str] = []
    if "worst-case" in lowered or "worst case" in lowered or "最坏情况" in text:
        fatal_tags.append("invalid_worst_case_construction")
    if "ledger_conflict" in lowered or "shape controllable by touch" in lowered or "形状可控" in text:
        ledger_conflicts.append("shape_controllable_by_touch")
    return fatal_tags, ledger_conflicts


def extract_audit_record(raw_text: str) -> Phase2bExtractedRecord:
    lowered = raw_text.lower()
    survives = not any(marker in lowered for marker in ("fail", "不通过", "fatal"))
    return Phase2bExtractedRecord(
        candidate_answer=None,
        target_interpretation=None,
        worst_case_claim=None,
        controllable_premises=[],
        survives=survives,
        fatal_error_tags=[] if survives else _normalize_fatal_tags(raw_text),
        ledger_conflicts=[],
        revised_answer_if_any=extract_numeric_answer(raw_text),
        missing_fields=[],
    )


def extract_attack_record(raw_text: str) -> Phase2bExtractedRecord:
    lowered = raw_text.lower()
    survives = not any(marker in lowered for marker in ("fail", "不通过", "fatal"))
    fatal_tags, ledger_conflicts = _normalize_attack_tags(raw_text)
    return Phase2bExtractedRecord(
        candidate_answer=None,
        target_interpretation=None,
        worst_case_claim=None,
        controllable_premises=[],
        survives=survives,
        fatal_error_tags=[] if survives else fatal_tags,
        ledger_conflicts=[] if survives else ledger_conflicts,
        revised_answer_if_any=extract_numeric_answer(raw_text),
        missing_fields=[],
    )
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest tests/test_phase2b_extract.py -v`

Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add proxy/phase2b_extract.py tests/test_phase2b_extract.py
git commit -m "feat: add phase2b tolerant extraction"
```

### Task 3: Implement Phase 2b Orchestration

**Files:**
- Create: `proxy/phase2b.py`
- Create: `tests/test_phase2b.py`
- Test: `tests/test_phase2b.py`

**Interfaces:**
- Consumes: `ProxyConfig`, `Phase2bConfig`, `Phase2bExecutionResult`, `Phase2bBranchDecision`, `RequestContext`, `CallBudget`, `UpstreamTransport`
- Consumes: `extract_branch_record(raw_text: str) -> Phase2bExtractedRecord`
- Produces: `is_phase2b_eligible(body: dict[str, Any], classification: ClassificationResult, config: ProxyConfig) -> bool`
- Produces: `run_phase2b(transport: UpstreamTransport, headers: dict[str, str], body: dict[str, Any], classification: ClassificationResult, config: ProxyConfig, request_id: str) -> Phase2bExecutionResult`
- Produces: `build_branch_prompt(body: dict[str, Any], branch_family: str) -> dict[str, Any]`
- Produces: `build_assumption_audit_prompt(body: dict[str, Any], branch_text: str, ledger: list[str]) -> dict[str, Any]`
- Produces: `build_worst_case_attack_prompt(body: dict[str, Any], branch_text: str, ledger: list[str]) -> dict[str, Any]`
- Produces: `build_final_compressor_prompt(body: dict[str, Any], survivor_text: str) -> dict[str, Any]`

- [ ] **Step 1: Write the failing orchestration tests**

```python
@pytest.mark.asyncio
async def test_run_phase2b_returns_baseline_when_all_branches_fail(config) -> None:
    from proxy.phase2b import run_phase2b

    phase2b_config = config.model_copy(deep=True)
    phase2b_config.phase2b.enabled = True
    classification = ClassificationResult("rewrite", 5, True, None, "最少 只输出 手感 保证")
    responses = [
        UpstreamResult(200, {}, b'{"type":"message","content":[{"type":"text","text":"14"}]}'),
        UpstreamResult(200, {}, b'{"type":"message","content":[{"type":"text","text":"bad branch"}]}'),
        UpstreamResult(200, {}, b'{"type":"message","content":[{"type":"text","text":"bad branch"}]}'),
        UpstreamResult(200, {}, b'{"type":"message","content":[{"type":"text","text":"bad branch"}]}'),
    ]
    transport = StubTransport(responses)

    result = await run_phase2b(
        transport=transport,
        headers={},
        body={"model": "m", "max_tokens": 4096, "stream": False, "messages": [{"role": "user", "content": "最少 只输出"}]},
        classification=classification,
        config=phase2b_config,
        request_id="req-1",
    )

    assert result.mode == "fallback_phase1"
    assert result.fallback_reason == "no_surviving_branches"


def test_is_phase2b_eligible_rejects_non_exact_output_request(config) -> None:
    from proxy.phase2b import is_phase2b_eligible

    phase2b_config = config.model_copy(deep=True)
    phase2b_config.phase2b.enabled = True
    body = {"model": "m", "max_tokens": 4096, "messages": [{"role": "user", "content": "Find the minimum and explain why."}]}
    classification = ClassificationResult("rewrite", 5, True, None, "Find the minimum and explain why.")

    assert is_phase2b_eligible(body, classification, phase2b_config) is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest tests/test_phase2b.py -v`

Expected: FAIL with missing `proxy.phase2b`.

- [ ] **Step 3: Implement eligibility and ledger helpers**

```python
def is_phase2b_eligible(body: dict[str, Any], classification: ClassificationResult, config: ProxyConfig) -> bool:
    surface = classification.effective_prompt_surface
    return (
        config.phase2b.enabled
        and classification.route in config.phase2b.trigger_on_routes
        and classification.replay_safe
        and not bool(body.get("stream"))
        and not bool(body.get("tools"))
        and body.get("tool_choice") in (None, "auto")
        and _is_single_turn_user_request(body)
        and _matches_non_negated_patterns(surface, config.classification.output_constraint_patterns)
        and not _requests_explanatory_output(surface)
        and not _requests_structured_output_format(surface)
        and not _matches_patterns(surface, config.classification.code_marker_patterns)
    )


def build_constraint_ledger(surface: str) -> list[str]:
    ledger = ["target is guarantee, not existence"]
    if "手感" in surface or "distinguishable" in surface.lower():
        ledger.append("shape is controllable by touch")
    if "苹果" in surface or "apple" in surface.lower():
        ledger.append("success condition requires apple and peach across opposite shapes")
    return ledger
```

- [ ] **Step 4: Implement branch, audit, attack, and final prompts**

```python
BRANCH_PROMPTS = {
    "premise_first": "Solve by locking the controllable premise first, then count under that premise.",
    "quota_first": "Solve by proposing explicit shape-bucket quotas and checking the worst-case allocation under those quotas.",
    "counterexample_first": "Solve by first trying to break low candidate answers with counterexamples before committing to a final count.",
}


def build_branch_prompt(body: dict[str, Any], branch_family: str) -> dict[str, Any]:
    request = deepcopy(body)
    request["stream"] = False
    request["system"] = _append_system_text(
        request.get("system"),
        f"Internal branch family: {branch_family}. {BRANCH_PROMPTS[branch_family]} Produce one solution branch with candidate_answer, target_interpretation, worst_case_claim, and controllable_premises. Brief plain text or JSON is acceptable."
    )
    return request


def build_assumption_audit_prompt(body: dict[str, Any], branch_text: str, ledger: list[str]) -> dict[str, Any]:
    request = deepcopy(body)
    request["messages"] = [{"role": "user", "content": f"Original task:\\n{_latest_user_text(body)}\\n\\nLedger:\\n- " + "\\n- ".join(ledger) + f"\\n\\nBranch:\\n{branch_text}\\n\\nCheck only for premise or target mistakes. Output PASS/FAIL plus fatal tags."}]
    request["stream"] = False
    request["system"] = "You are an internal assumption auditor. Do not solve from scratch."
    return request


def build_worst_case_attack_prompt(body: dict[str, Any], branch_text: str, ledger: list[str]) -> dict[str, Any]:
    request = deepcopy(body)
    request["messages"] = [{"role": "user", "content": f"Original task:\\n{_latest_user_text(body)}\\n\\nLedger:\\n- " + "\\n- ".join(ledger) + f"\\n\\nBranch:\\n{branch_text}\\n\\nAttack only the branch's worst-case construction. Output PASS/FAIL, worst-case fatal tags, and any ledger conflicts."}]
    request["stream"] = False
    request["system"] = "You are an internal worst-case attacker. Do not solve from scratch unless the branch fails."
    return request
```

- [ ] **Step 5: Implement orchestration and fallback**

```python
async def run_phase2b(... ) -> Phase2bExecutionResult:
    call_budget = CallBudget(config.phase2b.max_total_upstream_calls)
    baseline = await transport.send_with_retry(
        context=RequestContext(request_id=request_id, attempt=1, log_dir=Path(request_id) / "phase2b-baseline"),
        headers=headers,
        body=body,
        replay_safe=classification.replay_safe,
        stream=False,
        call_budget=call_budget,
    )
    if not 200 <= baseline.status_code < 300:
        return Phase2bExecutionResult("fallback_phase1", baseline, [], None, "baseline_failed")

    ledger = build_constraint_ledger(classification.effective_prompt_surface)
    branches = await _run_branch_generation(..., branch_families=config.phase2b.branch_families)
    decisions: list[Phase2bBranchDecision] = []
    survivors: list[tuple[str, str]] = []
    for branch_id, branch_text, branch_record in branches:
        if config.phase2b.enable_assumption_audit:
            audit_record = await _run_assumption_audit(...)
            if audit_record.survives is False:
                decisions.append(Phase2bBranchDecision(branch_id, False, audit_record.fatal_error_tags, audit_record.revised_answer_if_any, "assumption_audit_failed"))
                continue
        if config.phase2b.enable_worst_case_attack:
            attack_record = await _run_worst_case_attack(...)
            if attack_record.survives is False:
                decisions.append(Phase2bBranchDecision(branch_id, False, attack_record.fatal_error_tags, attack_record.revised_answer_if_any, "worst_case_attack_failed"))
                continue
        survivors.append((branch_id, branch_text))
        decisions.append(Phase2bBranchDecision(branch_id, True, [], branch_record.candidate_answer, "survived"))
    if not survivors:
        return Phase2bExecutionResult("fallback_phase1", baseline, decisions, None, "no_surviving_branches")
    downstream_payload = await _finalize_survivor(...)
    if downstream_payload is None:
        return Phase2bExecutionResult("fallback_phase1", baseline, decisions, None, "final_compression_failed")
    return Phase2bExecutionResult("phase2b", baseline, decisions, downstream_payload, None)
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest tests/test_phase2b.py -v`

Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add proxy/phase2b.py tests/test_phase2b.py
git commit -m "feat: add phase2b orchestration"
```

### Task 4: Wire Phase 2b Through The App, Eval Harness, And Docs

**Files:**
- Modify: `proxy/app.py`
- Modify: `proxy/evals.py`
- Modify: `README.md`
- Modify: `tests/test_app.py`
- Modify: `tests/test_evals.py`
- Test: `tests/test_app.py`
- Test: `tests/test_evals.py`

**Interfaces:**
- Consumes: `is_phase2_eligible(...) -> bool`, `run_phase2(...) -> Phase2ExecutionResult`
- Consumes: `is_phase2b_eligible(...) -> bool`, `run_phase2b(...) -> Phase2bExecutionResult`
- Produces: phase selector mode `claude_code_proxy_phase2b`
- Produces: app routing precedence `phase2b` before ordinary upstream passthrough, without disturbing streamed traffic
- Produces: `run_repeated_mode(mode: str, fixture: Path, config: ProxyConfig, repeat_count: int) -> list[dict[str, Any]]`

- [ ] **Step 1: Write the failing app and eval tests**

```python
def test_messages_return_phase2b_selected_answer_for_non_streamed_requests(config, monkeypatch) -> None:
    from proxy.schemas import Phase2bBranchDecision, Phase2bExecutionResult

    phase2b_config = config.model_copy(deep=True)
    phase2b_config.phase2b.enabled = True
    monkeypatch.setattr("proxy.app.is_phase2b_eligible", lambda *_: True)

    async def fake_run_phase2b(**kwargs):
        return Phase2bExecutionResult(
            "phase2b",
            UpstreamResult(200, {}, b'{"type":"message","content":[{"type":"text","text":"14"}]}'),
            [Phase2bBranchDecision("b1", True, [], "21", "survived")],
            {"type": "message", "id": "phase2b", "content": [{"type": "text", "text": "21"}]},
            None,
        )

    monkeypatch.setattr("proxy.app.run_phase2b", fake_run_phase2b)
    with TestClient(create_app(phase2b_config, transport=StubTransport(UpstreamResult(200, {}, b"{}")))) as client:
        response = client.post("/v1/messages", json={"model": "m", "max_tokens": 4096, "stream": False, "messages": [{"role": "user", "content": "最少 只输出"}]})

    assert response.status_code == 200
    assert response.json()["content"][0]["text"] == "21"


def test_run_mode_supports_phase2b_proxy_mode(config) -> None:
    from proxy.evals import run_mode

    result = run_mode("claude_code_proxy_phase2b", Path("proxy/fixtures/candy_question.json"), config, execute=False)
    assert result["request_headers"]["x-cc-proxy-phase"] == "phase2b"


def test_run_repeated_mode_records_phase2b_repeated_results(config, monkeypatch) -> None:
    from proxy.evals import run_repeated_mode

    monkeypatch.setattr(
        "proxy.evals.run_mode",
        lambda mode, fixture, config, execute=True: {
            "mode": mode,
            "fixture": str(fixture),
            "response_text": "21",
            "score": {"exact_match": True, "protocol_ok": True},
        },
    )

    results = run_repeated_mode(
        "claude_code_proxy_phase2b",
        Path("proxy/fixtures/candy_question.json"),
        config,
        repeat_count=3,
    )

    assert len(results) == 3
    assert all(item["response_text"] == "21" for item in results)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest tests/test_app.py -k phase2b -v && PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest tests/test_evals.py -k phase2b -v`

Expected: FAIL with missing phase2b routing and eval mode.

- [ ] **Step 3: Add phase selector and app routing**

```python
from proxy.phase2b import is_phase2b_eligible, run_phase2b


if not bool(outgoing_body.get("stream")):
    if phase_selector == "phase2b":
        should_run_phase2b = is_phase2b_eligible(outgoing_body, classification, active_config)
    elif phase_selector not in {"phase1", "phase2"}:
        should_run_phase2b = is_phase2b_eligible(outgoing_body, classification, active_config)

if should_run_phase2b:
    phase2b_result = await run_phase2b(...)
    if phase2b_result.downstream_payload is not None:
        return JSONResponse(status_code=200, content=phase2b_result.downstream_payload)
    status_code, content = _parse_upstream_json(phase2b_result.baseline_upstream, normalize_error=True)
    return JSONResponse(status_code=status_code, content=content)
```

- [ ] **Step 4: Add eval mode and README notes**

```python
PROXY_MODES = {
    "captured_claude_passthrough",
    "captured_claude_rewrite",
    "claude_code_proxy",
    "claude_code_proxy_phase1",
    "claude_code_proxy_phase2",
    "claude_code_proxy_phase2b",
}

PHASE_SELECTOR_BY_MODE = {
    "claude_code_proxy_phase1": "phase1",
    "claude_code_proxy_phase2": "phase2",
    "claude_code_proxy_phase2b": "phase2b",
}


def run_repeated_mode(mode: str, fixture: Path, config: ProxyConfig, repeat_count: int) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for _ in range(repeat_count):
        results.append(run_mode(mode, fixture, config, execute=True))
    return results
```

```md
## Phase 2b

Phase 2b is a narrow experimental path for single-turn exact-output reasoning prompts.

- It keeps one retained phase-1 baseline.
- It runs branch generation plus attack rounds before the final answer.
- It is intentionally high-cost in the first implementation.
- The first success target is the candy fixture reaching exact-match `21` in repeated runs.

### Acceptance Batch

```bash
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m proxy.evals \
  --mode claude_code_proxy_phase2b \
  --fixture proxy/fixtures/candy_question.json \
  --repeat 10
```
```

- [ ] **Step 5: Run focused tests**

Run: `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest tests/test_app.py tests/test_evals.py -k phase2b -v`

Expected: PASS

- [ ] **Step 6: Run broader regression**

Run: `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest tests/test_config.py tests/test_phase2b_extract.py tests/test_phase2b.py tests/test_app.py tests/test_evals.py -v`

Expected: PASS

- [ ] **Step 7: Run real-provider candy acceptance batch**

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m proxy.evals \
  --mode claude_code_proxy_phase2b \
  --fixture proxy/fixtures/candy_question.json \
  --repeat 10
```

Expected:

- All 10 runs complete without protocol failure.
- Exact-match `21` is returned in `10/10` runs against the configured xfyun backend.
- The task report records the aggregate counts plus any fallback-stage metadata that appeared during the run.

- [ ] **Step 8: Commit**

```bash
git add proxy/app.py proxy/evals.py README.md tests/test_app.py tests/test_evals.py
git commit -m "feat: wire phase2b through app and evals"
```

## Self-Review

- Spec coverage:
  - narrow single-turn exact-output scope is covered in Tasks 1, 3, and 4,
  - tolerant middle-round extraction is covered in Task 2,
  - branch/audit/attack/final-compression orchestration is covered in Task 3,
  - downstream protocol preservation and eval mode wiring are covered in Task 4,
  - candy-targeted validation support is covered by Task 4 eval changes and its broader regression step.
- Placeholder scan:
  - no `TODO`, `TBD`, or “implement later” placeholders remain,
  - each task includes exact files, interfaces, commands, and code snippets.
- Type consistency:
  - `Phase2bConfig`, `Phase2bExecutionResult`, `Phase2bBranchDecision`, and extraction function names are defined in earlier tasks before later tasks consume them.

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-07-18-third-party-reasoning-phase-2b-implementation.md`.
