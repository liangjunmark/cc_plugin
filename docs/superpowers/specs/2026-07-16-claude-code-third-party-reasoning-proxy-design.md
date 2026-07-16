# Claude Code Third-Party Reasoning Proxy Design

Date: 2026-07-16

## Objective

Design a minimal proxy layer that sits between Claude Code CLI and a third-party Anthropic-compatible API, with one immediate goal: diagnose why Claude Code degrades when pointed at the third-party provider and apply targeted request-side corrections that stabilize difficult single-turn reasoning without replacing the agent itself.

The first acceptance target is the candy-shape test question whose correct answer is `21`.

## Problem Statement

Current local project configuration points Claude Code at a third-party Anthropic-compatible endpoint and maps every Claude tier alias to the same backend model:

- `ANTHROPIC_BASE_URL=https://maas-coding-api.cn-huabei-1.xf-yun.com/anthropic`
- `ANTHROPIC_MODEL=astron-code-latest`
- all default model aliases also point to `astron-code-latest`

Observed behavior:

- Direct API calls to the provider are unstable on the test question and produce wrong answers.
- The same question can be answered correctly in a richer agent context.
- This suggests the third-party backend is not robust on its own, while the agent stack may add useful structure through system instructions, thinking configuration, formatting constraints, and request shaping.

The design goal is to preserve Claude Code's existing agent loop and protocol while inserting the smallest possible proxy that can:

- observe the exact request and response differences,
- normalize or rewrite only the parts that degrade on the third-party backend,
- prove whether request shaping alone can recover stable reasoning on the target task.

## Non-Goals

- Rebuilding Claude Code or replacing its tool loop
- Implementing multi-sample voting or answer arbitration in phase 1
- Faking tool use or synthesizing tool results
- Rewriting user questions
- Porting CodexCont's OpenAI Responses-specific continuation logic directly
- Solving general model quality issues unrelated to the Claude Code integration path

## Design Principles

- Preserve the agent: Claude Code or Codex remains the agent. The proxy only improves the transport and prompt shape.
- Preserve the protocol: expose standard Anthropic `POST /v1/messages` to the client.
- Prefer diagnosis before enhancement: phase 1 prioritizes evidence collection and minimal corrections over aggressive optimization.
- Make every rewrite inspectable: the proxy must log original and rewritten payloads for comparison.
- Keep changes reversible: each rewrite rule can be toggled independently.

## Why CodexCont Is Relevant

CodexCont demonstrates that a thin middleware layer can materially improve reasoning outcomes without replacing the upstream agent. Its useful design lessons are:

- transparent protocol-preserving proxying,
- detailed request and round logging,
- normalization between client and upstream expectations,
- targeted intervention only where the upstream path is known to distort model behavior.

Its Responses-specific continuation tricks are not directly portable to Anthropic Messages, so this design borrows the middleware philosophy rather than the exact continuation mechanism.

## Phase 1 Scope

Phase 1 delivers a minimal local proxy plus an evaluation harness.

The proxy will:

- accept Claude Code traffic via Anthropic-compatible `/v1/messages`,
- forward to the third-party upstream provider,
- record raw inbound requests and upstream responses,
- optionally normalize and minimally rewrite selected request fields,
- return a normal Anthropic-compatible response to the client.

The evaluation harness will:

- run the same test prompts through direct upstream API access,
- run them through the proxy with rewriting disabled,
- run them through the proxy with individual rewrite rules enabled,
- compare correctness, output stability, truncation behavior, and format compliance.

## Architecture

### 1. Listener

Responsibility:

- expose `POST /v1/messages`,
- preserve Anthropic-compatible request and response shape,
- provide a local base URL for Claude Code to target.

Behavior:

- reject malformed JSON with explicit error output,
- preserve status codes from upstream where possible,
- keep request IDs and timestamps in local logs.

### 2. Recorder

Responsibility:

- capture the evidence needed to explain degradation.

Artifacts per request:

- inbound raw payload from Claude Code or local test client,
- normalized payload after protocol cleanup,
- rewritten payload sent upstream,
- raw upstream response,
- normalized response returned downstream,
- metadata summary: prompt length, system length, content block count, declared thinking mode, max tokens, stop reason, output length.

Storage:

- line-delimited JSON or one JSON file per request,
- timestamped folders for reproducible diffs,
- no secrets written unless explicitly redacted-safe logging is disabled.

### 3. Normalizer

Responsibility:

- clean protocol shape without changing task meaning.

Initial normalization rules:

- standardize `system` into a consistent internal representation,
- flatten or sanitize message content blocks where the third-party backend mishandles mixed content structures,
- preserve or explicitly add required Anthropic version headers,
- keep `thinking` fields explicit rather than relying on backend defaults,
- normalize empty or omitted fields that cause provider ambiguity.

This layer is intended to remove compatibility noise before any semantic rewrite is attempted.

### 4. Rewriter

Responsibility:

- apply small, controlled changes to improve reasoning quality on the third-party backend while preserving Claude Code's agent behavior.

Phase 1 rewrite candidates:

1. `max_tokens` floor
   - Raise very small output limits to a configurable minimum when the task looks reasoning-heavy.
   - Purpose: reduce accidental early truncation.

2. Explicit thinking enablement
   - If the request lacks an explicit `thinking` block, optionally inject one.
   - If present but small, optionally raise the budget within configured bounds.
   - Purpose: test whether Claude Code's advantage partially comes from better-delimited reasoning mode.

3. Strict-format guardrail
   - For prompts that already request an exact output format, add a compact system suffix reinforcing format fidelity without changing the task.
   - Purpose: reduce backend drift on "only output a number" style questions.

4. System compression
   - When the inbound system prompt is excessively long or deeply layered, derive a compressed upstream system preamble that preserves operational instructions but reduces stack depth and verbosity.
   - Purpose: test the hypothesis that the third-party backend loses task focus when overloaded by a large instruction hierarchy.

5. Message canonicalization
   - Reorder or canonicalize system and user content only when needed to match the upstream provider's most reliable shape.
   - Purpose: reduce ambiguity introduced by permissive compatibility shims.

Phase 1 rewrite exclusions:

- no candidate-answer merging,
- no sampling ensembles,
- no synthetic assistant self-dialogue,
- no semantic rewriting of the user problem.

### 5. Evaluator

Responsibility:

- verify whether the proxy improves stability and correctness.

Initial test modes:

- `direct`: local script hits the third-party Anthropic-compatible upstream directly,
- `proxy_passthrough`: local script hits the proxy with all rewrites disabled,
- `proxy_rule_X`: local script hits the proxy with only one rewrite enabled,
- `proxy_combined`: local script hits the proxy with the recommended minimal rule set,
- `claude_code_proxy`: manual or scripted Claude Code run against the local proxy.

Primary metrics:

- answer correctness on the target problem,
- repeated-run stability at `temperature=0`,
- response completeness,
- format compliance,
- presence or absence of truncation,
- latency and token cost for each mode.

## Core Hypotheses

Phase 1 is meant to test the following hypotheses in order:

1. Claude Code sends stronger request structure than a simple hand-written direct API call.
2. Some of that useful structure is lost or distorted by the third-party Anthropic-compatible path.
3. A small set of request-shape corrections can recover stable reasoning on the target question without changing the agent.
4. If those corrections fail, the next phase should add heavier methods such as self-consistency, answer arbitration, or multi-call reasoning.

## Request Classification

Phase 1 should avoid broad heuristics that affect all traffic. Rewrite rules are activated only for requests that appear reasoning-heavy.

Initial routing signal:

- long single-turn math or logic question,
- explicit output constraints such as "only output a number",
- no tool use declared or expected,
- no file-editing or repo-navigation task markers.

If classification confidence is low, the request should default to passthrough plus logging.

## Error Handling

- Upstream transport errors return upstream-like failures and are logged with local context.
- If a rewrite rule throws or produces invalid Anthropic payload shape, the proxy falls back to normalized passthrough.
- If response normalization fails, return the raw upstream body when possible instead of fabricating a new structure.
- Logging failures must not block serving traffic.

## Security and Secrets

- API tokens must be loaded from environment or config, not written into the spec-generated code by default.
- Recorder should redact `x-api-key`, `authorization`, GitHub PATs, and any obviously secret-bearing fields by default.
- Request logging should be easy to disable for sensitive sessions.

## Verification Plan

### Test Question

Use the candy-shape problem as the first hard acceptance test. The expected answer is `21`.

### Repetition

Run at least 10 repeated calls per mode at `temperature=0`:

- direct upstream,
- proxy passthrough,
- proxy with each rewrite rule enabled independently,
- proxy with the best combined rule set.

### Success Criteria

Phase 1 is considered successful if all of the following hold:

- the proxy identifies at least one concrete request-shape difference that correlates with better reasoning,
- the proxy improves the target question from unstable wrong answers to stable correct output `21`,
- the improved path does not replace the agent or require multi-sample answer merging,
- the proxy still passes through ordinary Anthropic-compatible traffic without breaking protocol shape.

### Failure Criteria

Phase 1 is considered unsuccessful if:

- no observable request-shape factor explains the performance gap,
- minimal rewriting cannot stabilize the target question,
- the third-party backend remains unstable even under normalized and clarified requests,
- or protocol-preserving correction proves too weak, implying phase 2 is necessary.

## Phase 2 Trigger

Only move to phase 2 if phase 1 fails to stabilize the target question.

Possible phase 2 additions:

- multi-call self-consistency,
- candidate answer comparison,
- deterministic answer verification for narrow task classes,
- selective second-pass reasoning on suspected difficult prompts.

These are intentionally deferred because they solve the broader model weakness problem rather than the narrower Claude Code integration problem.

## Implementation Sketch

Suggested local structure:

```text
proxy/
  app.py                # HTTP listener
  config.py             # local config and toggles
  recorder.py           # request/response logging
  normalize.py          # protocol cleanup
  rewrite.py            # minimal reasoning-oriented rewrites
  upstream.py           # upstream transport helpers
  evals.py              # repeated-run evaluation driver
  fixtures/
    candy_question.json
```

The proxy should start as a local-only script or service, not as a final packaged Claude Code plugin.

## Open Questions Resolved In This Design

- The proxy may rewrite request content but must not change protocol shape.
- The first goal is diagnosis and minimal correction, not a production-ready plugin.
- Single-turn reasoning correctness is prioritized over speed.
- Multi-round or multi-sample answer merging is explicitly postponed.
- Claude Code and Codex are both in scope as clients, but the design is centered on preserving their existing agent capabilities rather than replacing them.

## Immediate Next Step

Write an implementation plan for the minimal proxy and evaluation harness, then build the logging-only path first before enabling any rewrite rules.
