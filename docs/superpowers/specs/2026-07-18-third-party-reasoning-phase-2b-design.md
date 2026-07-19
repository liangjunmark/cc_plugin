# Claude Code Third-Party Reasoning Phase 2b Design

Date: 2026-07-18

## Objective

Design a narrow, high-cost `phase2b` orchestration path for third-party Anthropic-compatible providers that improves difficult single-turn constrained reasoning when:

- `phase1` request shaping is insufficient,
- the existing `phase2` minimal design fails because same-model candidate generation and same-model adjudication collapse into the same wrong reasoning cluster,
- Claude Code CLI or Codex must remain the real agent.

The first acceptance target is intentionally narrow: on the candy-shape fixture, `phase2b` must produce the exact answer `21` in `10/10` repeated runs against the configured xfyun backend.

## Problem Statement

By July 18, 2026, the local evaluation evidence against `https://maas-coding-api.cn-huabei-1.xf-yun.com/anthropic` with model alias `astron-code-latest` supports these conclusions:

- direct single-shot calls are protocol-compatible but reasoning-unstable on the candy-shape fixture,
- `phase1` can occasionally recover the correct answer but is not stable,
- the current `phase2` minimal design does not improve this provider and can be worse than `phase1`,
- structured candidate requests often return fence-wrapped or truncated content instead of clean machine-parseable JSON,
- same-model adjudication does not recover the correct answer and instead converges on the wrong cluster, especially `25`.

This means the remaining gap is not only "format the request better" or "ask for JSON more clearly." The current failure mode is that the same provider repeatedly reinforces the same flawed reasoning structure. `phase2b` therefore must force the model to expose, attack, and discard its own bad reasoning branches before the final exact-output answer is emitted.

## Non-Goals

- Replacing Claude Code or Codex with a bespoke external agent
- Rewriting ordinary coding, tool, patch, shell, or multi-turn traffic
- Solving general third-party model weakness across all prompt types
- Guaranteeing low latency or low cost in the first `phase2b` iteration
- Requiring hidden reasoning-token APIs or provider-specific private capabilities
- Claiming general intelligence uplift from a single prompt-specific success
- Replacing `phase1`; `phase2b` is an opt-in experimental escalation path

## Design Principles

- Preserve the agent: Claude Code or Codex stays the agent; the proxy adds orchestration only on a narrow escalation path.
- Preserve the downstream contract: the client still receives one ordinary Anthropic-compatible non-streamed response.
- Prefer exposed reasoning over rigid formatting: middle rounds should reveal assumptions and failure modes, not optimize for perfect JSON.
- Attack reasoning early: do not wait until the end to adjudicate already-correlated wrong answers.
- Keep fallback clean: once the baseline exists, every local failure path must return the retained baseline response.
- Optimize for proving efficacy first: start with a high-cost path, then shrink cost only after the narrow acceptance target is met.

## Why Phase 2b Exists

The current `phase2` minimal design assumes:

- candidate diversity is enough to break wrong-answer clustering,
- candidate summaries can be made reliably machine-parseable,
- late adjudication can identify the best surviving answer.

The xfyun evidence contradicts those assumptions:

- multiple candidate solves still cluster into the same family of wrong answers,
- candidate formatting often fails before useful reasoning can be aggregated,
- same-model adjudication does not rescue the answer and instead stabilizes the wrong cluster.

`phase2b` changes the unit of work from "sample multiple answers and compare them" to "generate explicit reasoning branches, attack them for known failure modes, keep only branches that survive refutation, then compress the survivor into one exact-output answer."

## Scope

`phase2b` applies only to a narrow lane of requests:

- exactly one user turn,
- no assistant continuation state,
- no declared tools or tool results,
- no repo, file-edit, shell, patch, or coding intent,
- `stream=false`,
- exact-output or answer-only expectation,
- reasoning-heavy prompt shape already eligible for the current reasoning path.

This scope is deliberate. The immediate goal is to prove that same-provider self-correction can work on hard constrained reasoning without damaging ordinary Claude Code flows.

## High-Level Flow

```text
Claude Code / Codex
  -> phase1 classification and normalization
  -> if not phase2b-eligible: ordinary phase1 path
  -> if eligible:
       - execute retained phase1 baseline
       - generate N branch drafts
       - audit each branch for premise / target / guarantee mistakes
       - attack each branch's worst-case construction
       - keep only surviving branches
       - if needed, run one survivor-versus-survivor tie-break
       - compress the survivor into one exact-output downstream answer
       - on any orchestration failure, return retained baseline
```

## Eligibility Gate

`phase2b` should trigger only when all of the following hold:

- request is replay-safe under the existing phase-1 definition,
- request is classified into the current reasoning lane,
- request is non-streamed,
- request is single-turn and non-tool,
- request is answer-only or exact-output constrained,
- request is not a coding or filesystem task,
- local config explicitly enables `phase2b`,
- local cost budget allows the higher call envelope.

`phase2b` must not trigger for:

- ordinary Claude Code coding flows,
- tool execution flows,
- streamed requests,
- prompts that require structured downstream formats such as arbitrary JSON output for the user,
- prompts whose visible instruction surface explicitly asks for explanatory final output.

## State Machine

The minimal `phase2b` state machine has six states:

### 1. `eligible`

The request has passed the narrow gate and is allowed to enter `phase2b`.

### 2. `baseline_ready`

The proxy executes one retained `phase1`-style solve and stores:

- the full upstream Anthropic-compatible response,
- the observed answer if one can be extracted locally,
- recorder metadata for later comparison.

This baseline is the only guaranteed downstream fallback source.

### 3. `branches_ready`

The proxy launches `N` branch-generation calls. For the first implementation, `N` is fixed at `3` rather than the broader `3` to `5` experimental range so the call budget and branch families stay deterministic. These calls are not strict candidate JSON solves. They are branch drafts that expose:

- candidate answer,
- target-condition interpretation,
- worst-case construction,
- relied-on controllable premises.

### 4. `refuted`

Each branch is passed through one or more attack rounds:

- assumption audit,
- worst-case attack,
- optional short ledger check.

Each branch is then marked as either:

- survived,
- failed with fatal error tags,
- unusable due to extraction failure.

### 5. `survivors_ready`

Only survived branches remain. If there are:

- `0` survivors: fallback to baseline,
- `1` survivor: proceed directly to finalization,
- `2+` survivors: optionally run one survivor-to-survivor tie-break or direct finalization depending on config.

### 6. `finalized`

The proxy asks the provider to emit only the final answer from the surviving branch context. This is the only round that must satisfy the downstream exact-output requirement.

## Prompt Families

`phase2b` uses fixed internal prompt families rather than the current broad `constraint_reasoner` / `counterexample_reasoner` roles.

### 1. `branch_builder`

Job:

- produce one explicit solution branch,
- expose the branch structure even if the answer is wrong.

Required information:

- candidate answer,
- interpretation of the task goal,
- worst-case construction,
- controllable premise usage.

Output expectations:

- brief internal text is acceptable,
- strict JSON is not required,
- the branch must be locally extractable.

First-iteration branch families:

- `premise_first`: solve by explicitly anchoring controllable premises before counting,
- `quota_first`: solve by constructing bucket-wise draw quotas under worst-case pressure,
- `counterexample_first`: solve by first trying to break candidate low answers before committing.

The first implementation must use these distinct branch families rather than sending the same prompt three times with only a branch identifier changed.

### 2. `assumption_auditor`

Job:

- inspect a branch for known semantic mistakes.

Fixed checks:

- treating a controllable attribute as uncontrollable,
- turning a guarantee question into an existence question,
- misreading the target condition,
- silently dropping one half of a disjunctive success condition.

Output expectations:

- pass or fail,
- short failure tags,
- optional revised answer only if the failure is explicit.

### 3. `worst_case_attacker`

Job:

- attack the branch's worst-case construction instead of generating a fresh independent solution.

Required behavior:

- identify illegal or incomplete worst-case constructions,
- produce a stronger counterexample if the branch's construction is not actually worst-case,
- explicitly say when the branch survives.

### 4. `final_compressor`

Job:

- convert the surviving branch into the exact downstream format.

Required behavior:

- output only the final answer,
- no explanation,
- no reuse of branches that were already marked failed.

## Constraint Ledger

`phase2b` uses a short local ledger to anchor attack rounds. The ledger is not a symbolic solver. It is a compact list of prompt-hard facts that must not be dropped.

For the candy-shape task, the relevant ledger items are:

- shape is distinguishable by touch,
- distinguishable shape is a controllable selection dimension,
- the target is a guarantee, not an existence claim,
- the success condition is `(round apple + star peach) OR (star apple + round peach)`,
- worst-case reasoning must preserve the controllable-shape premise rather than collapsing into blind random draw assumptions.

The minimal design should derive a short ledger locally from classification and lightweight prompt-surface inspection. It does not need a general semantic parser.

## Local Data Model

`phase2b` should not reuse the current "strict JSON candidate or discard" model as its primary internal interface.

Instead, each round should persist three layers of local state.

### 1. Raw Artifact

Fields:

- `stage`
- `role`
- `raw_text`
- `status_code`
- `observed_answer`
- `parse_quality`

This preserves maximum diagnostic value and ensures format pollution is not mistaken for pure reasoning failure.

### 2. Extracted Record

Branch generation should attempt to extract:

- `candidate_answer`
- `target_interpretation`
- `worst_case_claim`
- `controllable_premises[]`
- `missing_fields[]`

Audit or attack rounds should attempt to extract:

- `survives`
- `fatal_error_tags[]`
- `ledger_conflicts[]`
- `revised_answer_if_any`
- `missing_fields[]`

### 3. Decision Record

The orchestration layer then decides:

- whether the branch remains usable,
- whether the branch failed only format expectations or failed semantically,
- whether the branch should advance to another attack round,
- whether the whole request should fallback to baseline.

## Parsing Strategy

Parsing should be tolerant in middle rounds and strict only at the final downstream boundary.

### Level 1: Structured Parse

Try explicit JSON or explicit tag-based parsing first.

### Level 2: Salvage Parse

If the reply includes:

- markdown fences,
- leading or trailing explanation,
- mild field-shape deviations,
- one extra wrapper layer,

strip the wrapper and salvage the useful fields.

### Level 3: Semantic Fallback

If structured extraction still fails, attempt to recover only:

- the best candidate numeric answer,
- whether the branch explicitly treated the key premise as controllable,
- whether the branch explicitly referred to worst-case, guarantee, or counterexample logic.

This allows a weak branch to be attacked rather than discarded before the attack stage begins.

## Decision Policy

The minimal `phase2b` policy is:

1. Always obtain the baseline first.
2. Generate `3` to `5` branches.
3. Run assumption audit on every usable branch.
4. Drop branches with fatal semantic failures.
5. Run worst-case attacker on the remaining branches.
6. Drop branches that fail attacker checks or conflict with the ledger.
7. If no branches survive, return the retained baseline.
8. If one branch survives, finalize from that branch.
9. If multiple branches survive, run one tie-break or survivor compression round.
10. If final compression fails, return the retained baseline.

The key invariant is that middle-round parsing failure is not automatically equivalent to branch death unless no semantically useful information can be recovered.

## Call Budget

The first `phase2b` iteration is intentionally high-cost.

Recommended starting envelope for the first implementation:

- `1` retained baseline call,
- `3` branch-builder calls,
- `3` assumption-auditor calls,
- `3` worst-case-attacker calls,
- `0` or `1` survivor tie-break call,
- `1` final-compressor call.

Initial ceiling:

- maximum total upstream calls per eligible request: `13`,
- no streaming support,
- timeout budget allowed to scale well beyond `phase2` minimal while still remaining locally bounded.

This is deliberately expensive because the near-term goal is to prove same-provider self-correction can work at all on the narrow acceptance target.

## Downstream Contract

`phase2b` remains invisible to Claude Code or Codex except for latency.

The downstream client must receive:

- one ordinary non-streamed Anthropic-compatible success response, or
- one Anthropic-compatible error envelope if the baseline itself fails before a usable fallback exists.

The downstream client must not receive:

- branch text,
- attack text,
- internal ledger text,
- multiple candidate outputs,
- a synthetic alternate protocol.

## Failure And Fallback Rules

Fallback policy:

1. If the request is not eligible, do not enter `phase2b`.
2. If the baseline solve fails, return the normal baseline result or baseline-shaped error.
3. If branch generation fails catastrophically, return the retained baseline.
4. If all branches fail extraction before meaningful attack is possible, return the retained baseline.
5. If all branches are semantically defeated, return the retained baseline.
6. If final compression fails or emits invalid downstream shape, return the retained baseline.

`phase2b` must never leak partial internals downstream.

## Config Surface

The minimal config additions should be explicit and narrow:

- `phase2b.enabled: bool`
- `phase2b.trigger_on_routes: list[str]`
- `phase2b.max_branch_count: int`
- `phase2b.branch_families: list[str]`
- `phase2b.enable_assumption_audit: bool`
- `phase2b.enable_worst_case_attack: bool`
- `phase2b.enable_ledger_checks: bool`
- `phase2b.max_total_upstream_calls: int`
- `phase2b.total_timeout_seconds: float`
- `phase2b.allow_tiebreak_round: bool`
- `phase2b.require_exact_output_requests: bool`

Validation rules should guarantee:

- `max_total_upstream_calls` can cover every enabled stage,
- `branch_families` length equals `max_branch_count` in the first implementation,
- `phase2b` cannot enable streaming,
- `phase2b` cannot activate on tool-enabled or multi-turn flows,
- `phase2b` remains behind explicit local opt-in.

## Why Phase 2b Is Better Matched To The Xfyun Failure Mode

The current xfyun evidence suggests:

- asking for strict candidate JSON increases both formatting burden and reasoning burden,
- multiple candidates still converge on the same wrong structure,
- same-model late adjudication often stabilizes the wrong answer instead of rejecting it.

`phase2b` changes the burden distribution:

- branch generation exposes reasoning structure without first forcing a perfect schema,
- auditing isolates premise and target-reading mistakes,
- attacker rounds focus on defeating the branch instead of generating another correlated answer,
- final exact-output compression happens only after the branch field has been narrowed.

This is a better fit for a provider that is more likely to fail by locking into one flawed reasoning pattern than by merely sampling random incorrect answers.

## Acceptance Criteria

The first `phase2b` acceptance target is intentionally narrow.

Primary acceptance:

- on the candy-shape fixture, `phase2b` must output exact-match `21` in `10/10` repeated runs against the configured xfyun provider,
- every successful run must remain Anthropic-protocol-compatible downstream.

Secondary component acceptance:

- middle rounds should usually produce usable extracted records rather than collapsing into format-only failure,
- fallback should remain available and valid on every failed orchestration path.

## Experimental Plan

The recommended evaluation ladder is:

### 1. Component Checks

Confirm that:

- branch generation produces extractable branch records,
- assumption audit can label the known premise/guarantee mistakes,
- worst-case attack can label invalid worst-case constructions,
- final compression emits exact-output-only downstream text.

### 2. End-To-End Candy Validation

Run repeated comparisons across:

- `direct`,
- `phase1`,
- current `phase2`,
- `phase2b`.

Record for each run:

- final answer,
- exact-match correctness,
- protocol compatibility,
- fallback usage,
- total call count,
- total latency,
- failure stage when incorrect.

### 3. Ablation

If `phase2b` reaches `10/10` on the candy-shape fixture, immediately test which parts are necessary by removing:

- assumption audit,
- worst-case attack,
- ledger checks,
- extra branches,
- tie-break.

The purpose of ablation is to identify the smallest effective version before expanding to a wider seed suite.

## Failure Criteria

The first `phase2b` iteration should be considered unsuccessful if any of the following hold:

- the candy-shape runs still converge on the same wrong cluster such as `25`,
- most middle rounds remain unusable due to extraction or orchestration failure,
- the attacker rounds do not reliably eliminate the bad branch family,
- `phase2b` only appears to improve by breaking protocol or by silently falling back most of the time,
- final compression corrupts otherwise-correct survivor conclusions.

## Risks

- Same-provider self-critique may still rationalize wrong branches instead of defeating them.
- High call counts may make local experimentation slow or expensive.
- Narrow prompt-family tuning may overfit the candy-shape task.
- Salvage parsing increases implementation complexity and must not weaken the final downstream protocol guarantees.

These risks are acceptable for the first `phase2b` iteration because the scope is explicitly diagnostic and narrow.

## Success Interpretation

If `phase2b` reaches the narrow acceptance target, the conclusion is not "the provider is now generally smarter." The narrower claim is:

- same-provider self-correction is not inherently impossible,
- the current `phase2` minimal design failed because its orchestration shape was mismatched to the provider,
- further work on cost reduction and broader evaluation is justified.

If `phase2b` fails even under this high-cost narrow setup, the likely conclusion is that same-provider self-rescue is not a strong route for this provider on this task family, and later designs should prefer cross-model adjudication or external verification.
