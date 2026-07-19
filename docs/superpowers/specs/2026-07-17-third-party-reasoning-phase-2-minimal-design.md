# Claude Code Third-Party Reasoning Phase 2 Minimal Design

Date: 2026-07-17

## Goal

Add a phase-2 enhancement path that improves third-party reasoning stability when phase 1 request shaping is not enough, while still preserving Claude Code CLI or Codex as the actual agent.

The immediate trigger for this design is the July 17, 2026 evaluation result against the configured xfyun Anthropic-compatible endpoint:

- direct runs on the candy-shape fixture returned wrong answers such as `29`, `33`, and `36`,
- phase-1 proxy rewrite runs still returned wrong answers such as `25` and `29`,
- even a stronger manual system scaffold sent directly upstream still returned wrong answers such as `22` and `12`.

That evidence is strong enough to treat the remaining gap as a model-stability problem rather than only a Claude Code request-shape problem.

## Why A Separate Phase 2

Phase 1 is still useful and should remain the default path:

- normalize protocol differences,
- preserve Anthropic-compatible request and response shape,
- preserve ordinary Claude Code or Codex agent flows,
- apply low-risk request-side corrections.

Phase 2 exists for a different job:

- increase reasoning reliability when one-shot inference is unstable,
- do so selectively rather than for all traffic,
- avoid replacing the whole agent with a new bespoke orchestration system.

Phase 2 therefore sits behind phase 1 rather than replacing it.

## Non-Goals

- Do not rewrite ordinary coding, tool-use, or multi-step agent traffic into a new workflow.
- Do not require hidden continuation rounds for every request.
- Do not assume access to provider-specific hidden reasoning tokens.
- Do not implement domain-specific symbolic solvers as the primary strategy.
- Do not claim general intelligence uplift from a single prompt-specific patch.

## Recommended Minimal Approach

The recommended phase-2 design is:

1. Keep the existing phase-1 proxy as the ingress path.
2. Trigger phase 2 only for replay-safe, reasoning-heavy, non-tool requests.
3. For eligible requests, first execute one retained phase-1 baseline solve and keep its full upstream result in memory as the fallback source.
4. Fan out a small number of separate structured candidate solves against the same upstream provider.
5. Force each candidate to return a structured reasoning summary in a machine-parseable schema.
6. Run a final aggregation step that compares candidate summaries and emits one downstream Anthropic-compatible answer.
7. If the phase-2 path fails at any step after the baseline exists, fall back cleanly to that retained phase-1 baseline result rather than breaking protocol.

This is preferred over simple majority voting because wrong answers can cluster. The design must compare intermediate reasoning claims, not only final numbers.

## High-Level Flow

```text
Claude Code / Codex
  -> phase-1 proxy classification and normalization
  -> if not eligible: ordinary passthrough or phase-1 rewrite
  -> if eligible:
       - execute retained phase-1 baseline solve
       - build phase-2 solve plan
       - launch N structured candidate solves
       - parse candidate JSON summaries
       - aggregate candidates
       - emit one final Anthropic-compatible response
```

## Eligibility Gate

Phase 2 should trigger only when all of the following hold:

- request is replay-safe under the existing phase-1 definition,
- request is classified as `rewrite` by phase-1 classification,
- request is non-streamed, meaning inbound `stream=true` requests stay on phase 1 in the minimal design,
- request has no `tool_use`, `tool_result`, or assistant continuation state,
- request is not marked by repo, shell, patch, file-edit, or command-execution patterns,
- request asks for nontrivial reasoning rather than simple retrieval or formatting only,
- request is allowed by a local cost and latency budget.

Phase 2 must not trigger for normal Claude Code coding flows. Its target is the same class of hard single-turn reasoning requests that already failed under phase 1.

Streaming note:

- the minimal phase-2 path does not support inbound Anthropic SSE passthrough,
- `stream=true` requests must stay on phase 1,
- a future phase can add synthetic downstream SSE only with an explicit separate protocol design.

## Candidate Solve Strategy

### Candidate Count

Start with `2` structured candidate solves, plus `1` retained phase-1 baseline fallback solve.

Rationale:

- `3` total upstream solve attempts before adjudication is enough to expose instability without causing the cost explosion of `5+`,
- the retained baseline preserves a normal phase-1 fallback while the two structured candidates provide controlled diversity,
- it keeps the first implementation small.

### Candidate Diversity

Candidate requests should keep the same user payload and mostly the same normalized request body, but vary only a narrow hidden solve scaffold. The objective is controlled diversity, not prompt roulette.

Recommended candidate roles:

- `constraint_reasoner`: emphasizes explicit constraint inventory and worst-case reasoning,
- `counterexample_reasoner`: emphasizes searching for a violating construction before finalizing the answer.

All candidate prompts must preserve the original task semantics. They may change hidden reasoning instructions, but not the user-visible problem.

The retained phase-1 baseline solve is not a structured candidate. It stays in the original Anthropic-compatible response shape so it can be returned unchanged on fallback.

### Candidate Output Schema

Each candidate must be asked to return strict JSON only:

```json
{
  "final_answer": "string",
  "answer_type": "number|string|boolean|text",
  "constraint_summary": [
    "string"
  ],
  "critical_steps": [
    "string"
  ],
  "counterexample_checked": true,
  "confidence": "low|medium|high"
}
```

Schema rules:

- `final_answer` is mandatory,
- `constraint_summary` must be short and task-relevant,
- `critical_steps` should summarize reasoning checkpoints rather than a full chain of thought,
- `counterexample_checked` signals whether the candidate explicitly tried to break its own answer,
- `confidence` is advisory only and must not dominate aggregation by itself.

Candidate normalization rules:

- each parsed candidate must retain both `raw_final_answer` and `canonical_final_answer`,
- the aggregator compares `canonical_final_answer`, not the raw string,
- leading and trailing whitespace must be stripped before comparison,
- if `answer_type = "number"`, numerically equivalent renderings such as `21`, `21.0`, and `021` must compare equal,
- if `answer_type = "boolean"`, case-insensitive equivalents such as `true` and `TRUE` must compare equal,
- if canonicalization fails for the declared `answer_type`, the candidate remains parseable but its answer must be marked `non_canonical` in local metadata and treated as lower-quality evidence.

This is not hidden chain-of-thought recovery. It is a compact, explicit reasoning artifact designed for comparison.

Baseline note:

- the retained phase-1 baseline response is preserved as raw fallback output, not coerced into candidate JSON,
- the proxy may extract a best-effort local `baseline_observed_answer` from that raw response for recorder metadata,
- `baseline_observed_answer` is advisory only and must not be treated as a structured candidate in aggregation.

## Aggregation Strategy

### Aggregator Input

The aggregator receives:

- the original user request,
- the phase-1 normalized request context,
- the retained phase-1 baseline result,
- the parsed candidate summaries,
- candidate parse failures if any.

### Aggregator Job

The aggregator must:

- compare candidate `canonical_final_answer` values,
- compare whether their `constraint_summary` sets are materially consistent,
- detect when a candidate obviously ignored a key constraint,
- treat the retained baseline as fallback-only rather than as a vote-bearing candidate,
- prefer candidates whose reasoning survives contradiction checks,
- emit one final answer in the user-requested format.

### Aggregator Decision Policy

The minimal policy is:

1. If at least `2` candidates agree on the same `canonical_final_answer` and their constraint summaries are materially compatible, choose that answer.
2. If at least `2` candidates agree on the same `canonical_final_answer` but their summaries conflict on a key constraint, do not auto-select; run adjudication if budget allows, otherwise fall back to the retained phase-1 baseline result.
3. If `2` parsed candidates disagree, whether or not another candidate failed to parse, run adjudication if budget allows, otherwise fall back to the retained phase-1 baseline result.
4. If only `1` structured candidate parsed successfully, abandon phase 2 and fall back to the retained phase-1 baseline result.
5. If a majority answer is present only in `non_canonical` form while a minority answer is canonical, do not auto-select the non-canonical majority; run adjudication if budget allows, otherwise fall back to the retained phase-1 baseline result.
6. If the adjudicator fails to emit a valid answer, fall back to the retained phase-1 baseline result.

This makes the system robust against clustered formatting failures and low-quality single candidates.

Decision precedence:

1. parse validity,
2. canonical answer agreement,
3. summary compatibility,
4. adjudication availability,
5. fallback to retained baseline.

### Adjudication Call

The adjudication call is allowed to be another upstream request. It should be used only on disagreement, not on the happy path.

Its prompt should:

- list the parsed candidate summaries,
- instruct the model to reject candidates that violate explicit constraints,
- require a strict local JSON result containing `final_answer`, `answer_type`, and a short `decision_summary`,
- keep `decision_summary` for local proxy use only,
- allow the proxy to emit only `final_answer` downstream when the original request requires exact-output formatting.

## Downstream Response Contract

For the minimal design, phase 2 runs only on non-streamed requests and returns one ordinary non-streamed Anthropic-compatible response to Claude Code or Codex.

The downstream client must not see:

- multiple candidate responses,
- internal JSON candidate artifacts,
- an alternate non-Anthropic protocol.

Streaming contract:

- if the inbound request asks for `stream=true`, the proxy must bypass phase 2 and stay on phase 1,
- the minimal phase-2 design must not buffer a streamed request and then emit a synthetic non-streamed reply,
- the minimal phase-2 design must not attempt partial synthetic SSE emission.

Recorder artifacts may store phase-2 internals locally, subject to the same redaction rules as phase 1.

## Failure And Fallback Rules

Phase 2 must degrade safely.

Fallback order:

1. If the eligibility gate fails: do not enter phase 2.
2. If the retained phase-1 baseline solve itself fails upstream, phase 2 is abandoned and the proxy returns the normal phase-1 result or error envelope from that baseline attempt.
3. If candidate fan-out transport fails before enough candidates are collected: return the retained phase-1 baseline result.
4. If candidate parsing fails for too many candidates: return the retained phase-1 baseline result.
5. If aggregation cannot produce one valid answer: return the retained phase-1 baseline result.
6. If local phase-2 orchestration fails after the baseline exists, return the retained phase-1 baseline result.

The proxy must never stream partial phase-2 candidate internals downstream.

## Cost And Latency Guardrails

Phase 2 is allowed to cost more than phase 1, but it must stay bounded.

Initial guardrails:

- structured candidate solve count: `2`,
- retained fallback baseline solve count: `1`,
- maximum adjudication calls: `1`,
- maximum total upstream calls per eligible request: `4`,
- default phase-2 timeout budget: `3x` phase-1 single-call timeout ceiling,
- default phase-2 concurrency: `2` in flight at once, not full unbounded fan-out,
- phase 2 disabled by default unless explicitly turned on.

If the local budget would be exceeded, the proxy should skip phase 2 and stay on phase 1.

## Configuration Schema

Add a new configuration group:

```text
phase2.enabled: bool
phase2.trigger_on_routes: list[str]
phase2.sample_count: int
phase2.max_adjudication_calls: int
phase2.max_parallelism: int
phase2.total_timeout_seconds: float
phase2.require_json_candidates: bool
phase2.candidate_roles: list[str]
phase2.max_candidate_output_tokens: int
phase2.max_total_upstream_calls: int
phase2.fallback_to_phase1_on_failure: bool
phase2.cost_budget_multiplier: float
phase2.allow_streaming_requests: bool
```

Safe defaults:

- `phase2.enabled = false`,
- `phase2.trigger_on_routes = ["rewrite"]`,
- `phase2.sample_count = 2`,
- `phase2.max_adjudication_calls = 1`,
- `phase2.max_parallelism = 2`,
- `phase2.total_timeout_seconds = 180.0`,
- `phase2.require_json_candidates = true`,
- `phase2.candidate_roles = ["constraint_reasoner", "counterexample_reasoner"]`,
- `phase2.max_candidate_output_tokens = 512`,
- `phase2.max_total_upstream_calls = 4`,
- `phase2.fallback_to_phase1_on_failure = true`,
- `phase2.cost_budget_multiplier = 2.0`,
- `phase2.allow_streaming_requests = false`.

Validation rules:

- `phase2.sample_count` must be `>= 2` and `<= 5`,
- `phase2.max_adjudication_calls` must be `0` or `1`,
- `phase2.max_parallelism` must be between `1` and `phase2.sample_count`,
- `phase2.candidate_roles` must contain only known role identifiers and its length must equal `phase2.sample_count`,
- `phase2.max_candidate_output_tokens` must be greater than `0`,
- `phase2.max_total_upstream_calls` must be at least `1 + phase2.sample_count + phase2.max_adjudication_calls`,
- `phase2.total_timeout_seconds` must be greater than `0.0`,
- `phase2.cost_budget_multiplier` must be greater than or equal to `1.0`,
- `phase2.allow_streaming_requests` must remain `false` in the minimal design; enabling streamed phase 2 requires a separate protocol extension spec.

Budget interpretation:

- `phase2.sample_count` counts structured candidate solves only and does not include the retained phase-1 baseline solve,
- `phase2.max_total_upstream_calls` counts the retained baseline solve, all structured candidates, and any adjudication call,
- `phase2.max_total_upstream_calls` therefore must be at least `1 + phase2.sample_count + phase2.max_adjudication_calls`,
- `phase2.cost_budget_multiplier` limits the estimated total phase-2 output-token budget relative to the retained phase-1 baseline solve budget; if the estimated phase-2 budget would exceed that multiplier, the proxy must skip phase 2 and keep the baseline result.

## Implementation Boundaries

Suggested additional modules:

```text
proxy/
  phase2.py         # eligibility, fan-out orchestration, aggregation
  candidate.py      # candidate prompt construction and JSON parsing
```

Responsibilities:

- `phase2.py`
  - decides whether phase 2 runs,
  - manages candidate solve execution,
  - manages aggregation and fallback.

- `candidate.py`
  - builds hidden candidate scaffolds,
  - validates candidate JSON,
  - normalizes candidate summaries into typed local objects.

This split keeps candidate parsing separate from request transport and keeps phase-2 orchestration isolated from phase-1 logic.

## Evaluation Design

The minimum evaluation suite should include:

- the candy-shape fixture with exact-output scoring,
- the candy-shape fixture with explanation-allowed scoring,
- at least `2` non-candy reasoning prompts requiring constraint handling,
- at least `1` negative-control prompt that should stay on phase 1,
- at least `1` captured real Claude Code request replay sample outside the candy domain.

Comparison modes should include:

- `direct`,
- `claude_code_proxy_phase1`,
- `claude_code_proxy_phase2`.

Report separately:

- reasoning correctness,
- exact-format compliance,
- protocol compatibility,
- median and p95 latency,
- total upstream call count,
- total token usage when available.

Branch coverage requirements:

- at least one test where only one structured candidate parses successfully,
- at least one test where two candidates agree but their summaries conflict on a key constraint,
- at least one test where two parsed candidates disagree and a third candidate is missing because of parse failure or transport failure,
- at least one test where a majority answer is non-canonical and a minority answer is canonical,
- at least one exact-output adjudication test verifying that downstream output remains answer-only even when the local adjudicator returns a `decision_summary`.

## Acceptance Criteria

The minimal phase-2 design is successful only if all of the following hold:

- on the candy-shape exact-output fixture, `claude_code_proxy_phase2` reaches at least `9/10` correct outputs of `21` in one batch and repeats that in a second batch,
- on the candy-shape explanation-allowed fixture, extracted answers also reach at least `9/10` correct outputs of `21`,
- at least `2` non-candy reasoning prompts improve or preserve correctness relative to `claude_code_proxy_phase1`,
- no new protocol-compatibility failures are introduced,
- phase 2 never triggers on ordinary coding or tool-use traffic in the validation set,
- median latency and cost increase are reported and remain within the configured budget,
- at least one improvement claim is backed by the same upstream provider and same fixture, so the uplift cannot be attributed only to provider switching.

## Failure Criteria

Phase 2 should be considered unsuccessful if:

- it only fixes the candy fixture through one narrow hidden prompt but does not improve the broader reasoning set,
- it improves exact-format output while reasoning correctness remains unstable,
- it materially degrades coding or tool-use traffic due to accidental triggering,
- it requires too many retries or adjudication rounds to stay practical,
- or it still cannot stabilize the target fixture despite structured multi-candidate aggregation.

## Rationale

This design is the smallest phase-2 step that addresses the actual empirical failure mode seen on July 17, 2026:

- phase-1 protocol and prompt cleanup helps classification and request hygiene,
- but one-shot third-party inference still drifts,
- therefore the next rational step is not more random rewrite tweaks,
- it is controlled multi-candidate reasoning with structured comparison and safe fallback.

## Immediate Next Step

Write an implementation plan for the phase-2 minimal path, starting with:

1. typed candidate schema and parser,
2. phase-2 config and validation,
3. fan-out orchestration with mocked upstream tests,
4. evaluator extension for `claude_code_proxy_phase2`,
5. real-provider validation against the xfyun endpoint and at least one second provider.
