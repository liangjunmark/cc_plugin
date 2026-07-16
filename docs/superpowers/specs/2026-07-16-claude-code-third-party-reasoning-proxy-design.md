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

Reference: `https://github.com/neteroster/CodexCont`

CodexCont demonstrates that a thin middleware layer can materially improve reasoning outcomes without replacing the upstream agent. Its useful design lessons are:

- transparent protocol-preserving proxying,
- detailed request and round logging,
- normalization between client and upstream expectations,
- targeted intervention only where the upstream path is known to distort model behavior.

Its Responses-specific continuation tricks are not directly portable to Anthropic Messages. In this document, "Responses-specific continuation" means OpenAI Responses API behaviors such as hidden continuation rounds, commentary-phase continuation markers, and reasoning-token-driven truncation recovery. Anthropic Messages does not expose the same control surface, so this design borrows the middleware philosophy rather than the exact continuation mechanism.

## Phase 1 Scope

Phase 1 delivers a minimal local proxy plus an evaluation harness.

The proxy will:

- accept Claude Code traffic via Anthropic-compatible `/v1/messages`,
- forward to the third-party upstream provider,
- record raw inbound requests and upstream responses,
- optionally normalize and minimally rewrite selected request fields,
- return an Anthropic-compatible success shape or Anthropic-compatible error envelope whenever no downstream response bytes have been emitted yet,
- once downstream streaming has begun, preserve stream continuity as far as possible and never attempt to retroactively replace a partial stream with a new full error envelope.

The evaluation harness will:

- run the same test prompts through direct upstream API access,
- replay captured raw Claude Code requests directly to the third-party upstream without proxy rewrites,
- replay the same captured raw Claude Code requests through proxy passthrough and proxy rewrite modes,
- run them through the proxy with rewriting disabled,
- run them through the proxy with individual rewrite rules enabled,
- compare correctness, output stability, truncation behavior, and format compliance.

Replay fidelity rule:

- apples-to-apples replay means every compared replay mode starts from the same captured raw request artifact,
- for `captured_claude_raw_upstream` and `captured_claude_passthrough`, the captured request body must be reused byte-for-byte,
- semantic request headers captured from Claude Code such as `anthropic-version`, `anthropic-beta`, and non-auth request-correlation headers must be preserved across replay modes unless the mode being tested explicitly changes them,
- hop-by-hop and proxy-self-managed headers must be regenerated consistently for every replay mode,
- auth credentials may be substituted from local config, but the substituted credential strategy must be identical across the compared replay modes.
- `captured_claude_raw_upstream` is the canonical baseline and must bypass normalization and rewrite logic entirely,
- `captured_claude_passthrough` may only apply transport-necessary header regeneration and auth substitution; it must not alter the captured body or semantic headers,
- `captured_claude_rewrite` and `proxy_rule_X` must derive from that same captured raw request artifact, but they may apply semantic normalization or rewrite behavior because they are experimental treatment modes rather than fidelity baselines,
- any deviation from these boundaries invalidates apples-to-apples attribution for that replay comparison.

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
- secrets redacted by default; raw unredacted logging must require an explicit unsafe override.

### 3. Normalizer

Responsibility:

- clean protocol shape without changing task meaning.

Initial normalization rules:

- standardize `system` into a consistent internal representation,
- flatten or sanitize message content blocks where the third-party backend mishandles mixed content structures,
- preserve or explicitly add required Anthropic version headers,
- forward end-to-end Anthropic request headers required for behavior parity, specifically `anthropic-version`, `anthropic-beta`, and tracing or request-correlation headers that do not contain credentials,
- keep `thinking` fields explicit rather than relying on backend defaults, meaning the proxy will either preserve the original `thinking` object verbatim or insert a concrete `thinking` object when a rewrite rule enables it,
- normalize empty or omitted fields that cause provider ambiguity.

Header forwarding policy:

- hop-by-hop headers such as `connection` and `transfer-encoding` must not be forwarded,
- transport-derived headers such as `host` and `content-length` must be regenerated according to the actual outbound request,
- `accept-encoding` should be forwarded by default when the proxy can transparently preserve upstream content-coding behavior; otherwise the proxy must explicitly normalize compression behavior and record that choice in metadata,
- proxy-self-managed transport negotiation headers such as `te` and `upgrade` must be regenerated or stripped according to the proxy's actual outbound transport mode,
- auth headers may be rewritten by local config but must still be represented in recorder metadata as redacted values,
- unknown non-hop-by-hop headers should be forwarded by default unless they are explicitly blocked for safety,
- recorder logging for forwarded headers is deny-by-default: only an explicit safe-header allowlist may be recorded in cleartext; all other forwarded headers must be redacted in logs unless raw logging has been explicitly enabled through the unsafe override path.

This layer is intended to remove compatibility noise before any semantic rewrite is attempted.

Capability negotiation policy:

- passthrough mode must preserve the client's original Anthropic feature surface as closely as possible,
- the proxy must not inject or force optional Anthropic-compatible fields such as `thinking` or additional beta headers unless the upstream capability probe or a prior successful request for the same provider profile indicates support,
- if capability support is unknown, the proxy must prefer preservation over expansion: keep fields the client already sent, but do not add new optional fields,
- upstream capability detection should be cached per provider profile and recorded in metadata so rewrite decisions are explainable.

Capability probe policy:

- passive evidence is preferred: if the client already sent an optional field such as `thinking` and the provider both accepted the request and returned positive evidence of understanding that field family, only then mark it as supported for the current provider profile,
- active probing is allowed only in dedicated evaluation mode, not inline on normal Claude Code traffic,
- the active probe must be a minimal synthetic request whose only purpose is testing support for a specific optional field family,
- explicit thinking injection is disabled until support is established by passive evidence, a successful active probe, or an explicit local allowlist entry created from prior verified evaluation,
- phase 1 does not attempt to bootstrap `thinking` support opportunistically inside normal Claude Code traffic; operators who want to test that rewrite must first establish support in evaluation mode or via a verified allowlist entry.

Positive evidence for `thinking` support must include at least one of:

- a response block type, usage field, or provider-specific metadata explicitly indicating thinking or reasoning support,
- a successful dedicated probe whose only material difference is the inclusion of a `thinking` field and whose outcome is recorded as support-confirming,
- an explicit provider-profile allowlist entry set by local configuration after prior verification.

### 4. Rewriter

Responsibility:

- apply small, controlled changes to improve reasoning quality on the third-party backend while preserving Claude Code's agent behavior.

Phase 1 request pipeline:

1. protocol normalization,
2. classification gate,
3. rewrite sequence:
   - `max_tokens` floor,
   - explicit thinking enablement,
   - message canonicalization,
   - strict-format guardrail,
   - system compression.

Interaction policy:

- Later rules may inspect earlier rewrites but must not silently drop them without emitting an explicit recorder event describing what was removed or transformed.
- `strict-format guardrail` appends to the effective upstream system prompt after canonicalization.
- `system compression` runs last and may transform earlier system text, but it must preserve explicit guardrail intent and emit a compression summary in recorder metadata.
- If a rule cannot preserve the operational intent of an earlier rule, it must abort and fall back to normalized passthrough for that request.
- Any rule-triggered fallback to normalized passthrough must log the failing rule name, the earlier rule it conflicted with if any, and a short machine-readable reason code.

Conflict detection policy:

- syntactic conflict: a rewrite would produce an invalid Anthropic payload shape, duplicate mutually exclusive fields, or malformed content block structure,
- semantic conflict: a rewrite would remove or invert an earlier explicit operational instruction such as "only output a number" or "preserve tool declaration state",
- on conflict, the client still receives a normal streamed response from normalized passthrough; the proxy does not surface a client-visible error unless passthrough itself fails,
- recorder logs must store `fallback_rule`, `conflicted_rule`, `reason_code`, and a human-readable summary.

Phase 1 rewrite candidates:

1. `max_tokens` floor
   - Raise very small output limits to a configurable minimum when the task looks reasoning-heavy.
   - Purpose: reduce accidental early truncation.

2. Explicit thinking enablement
   - If the request lacks an explicit `thinking` block, optionally inject one.
   - If present but small, optionally raise the budget within configured bounds.
   - Purpose: test whether Claude Code's advantage partially comes from better-delimited reasoning mode.
   - Risk level: high. The upstream provider may ignore, reinterpret, or over-consume tokens through this field.
   - Eligibility: only when `thinking` support is already known for the current provider profile.

3. Strict-format guardrail
   - For prompts that already request an exact output format, add a compact system suffix reinforcing format fidelity without changing the task.
   - Purpose: reduce backend drift on "only output a number" style questions.

4. System compression
   - When the inbound system prompt is excessively long or deeply layered, derive a compressed upstream system preamble that preserves operational instructions but reduces stack depth and verbosity.
   - Purpose: test the hypothesis that the third-party backend loses task focus when overloaded by a large instruction hierarchy.
   - Risk level: high. This is the most behavior-changing rewrite and must be off by default until the lighter rules are measured.

5. Message canonicalization
   - Reorder or canonicalize system and user content only when needed to match the upstream provider's most reliable shape.
   - Allowed transformations in phase 1:
     - flatten adjacent text blocks within the same message,
     - normalize equivalent `system` representations into one canonical internal form,
     - preserve relative ordering of user-authored semantic text,
     - preserve tool-related blocks verbatim when present.
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
- `direct_anthropic`: optional baseline mode against the standard Anthropic API when official credentials are available,
- `captured_claude_raw_upstream`: a captured raw Claude Code request is replayed directly to the third-party upstream with no proxy rewrite logic,
- `captured_claude_passthrough`: the same captured raw Claude Code request is replayed through the proxy with rewrites disabled,
- `captured_claude_rewrite`: the same captured raw Claude Code request is replayed through the proxy with selected rewrites enabled,
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

### 6. Upstream Transport

Responsibility:

- own the HTTP transport to the third-party provider,
- map local timeout and retry policy to upstream calls,
- preserve streaming semantics selected by the listener,
- translate upstream transport failures into recorder-visible local events.

Phase 1 transport policy:

- one initial upstream HTTP attempt per downstream Claude Code request,
- no hidden multi-call continuation,
- at most one retry total per logical request, and only for replay-safe requests,
- therefore phase 1 permits at most two serial upstream attempts for the same logical request lineage: the initial attempt plus one retry from the shared retry budget,
- no retry after a partial downstream stream has already been emitted.

Retry safety policy:

- requests containing tool-state blocks, assistant-generated tool plans, or any evidence of an in-progress agent loop are not retryable in phase 1,
- when retry is disabled for safety, the proxy must surface the upstream failure directly rather than risking duplicate agent behavior or duplicate billing,
- there is no generic idempotency guarantee from the third-party upstream provider, so the proxy must assume retries can duplicate effects unless the request is explicitly classified as replay-safe,
- connection-reset, upstream `502/503/504`, and pre-stream timeout all spend from the same single retry budget; they do not stack.

Replay-safe definition:

- a request is replay-safe only if all of the following hold:
  - it contains no `tool_use`, `tool_result`, `tool_choice`, or equivalent tool-state blocks,
  - it contains no assistant-authored continuation state that would change meaning if resent,
  - it contains no provider-visible idempotency-sensitive side-effect instruction such as "apply this patch now", "run this command now", or equivalent execution intent,
  - it is not part of an in-progress multi-step agent loop inferred from prior assistant/tool state in the same request body,
  - resending the exact request body to the upstream provider would, by design intent, be observational rather than state-changing.
- replay-safe is a conjunctive classification: if any required condition is unknown, the request is treated as not replay-safe.
- replay-safe must be computed once per logical request before retry or rewrite decisions are made and recorded in request metadata with the specific failing condition when false.

Loop prevention policy:

- on startup, the proxy must reject configuration where `upstream.base_url` resolves to the same effective host and port as the local listener,
- on each request, if the computed upstream target matches the local listener address after normalization, fail closed with a local configuration error instead of forwarding,
- the proxy should stamp a per-process signed internal hop marker such as `x-cc-proxy-hop` plus `x-cc-proxy-sig`; any inbound request already carrying a valid marker pair for the current process must be rejected to avoid accidental proxy recursion through external rewrites.

## Core Hypotheses

Phase 1 is meant to test the following hypotheses in order:

1. Claude Code sends stronger request structure than a simple hand-written direct API call.
2. Some of that useful structure is lost or distorted by the third-party Anthropic-compatible path.
3. A small set of request-shape corrections can recover stable reasoning on the target question without changing the agent.
4. If those corrections fail, the next phase should add heavier methods such as self-consistency, answer arbitration, or multi-call reasoning.

## Request Classification

Phase 1 has two parallel tracks:

- observation track: all Claude Code traffic is recorded, normalized for diagnostics, and can be replayed for apples-to-apples comparison,
- rewrite track: only requests that pass the rewrite gate receive semantic rewrites.

This separation is intentional. The proxy must diagnose the real Claude Code degradation path across all traffic, while keeping semantic rewrites narrowly scoped in phase 1.

Rewrite rules are activated only for requests that pass both hard gates and the reasoning score threshold.

Classification inputs:

- the request must expose an effective prompt surface that can be flattened into plain text,
- the effective prompt surface consists of:
  - all `system` text,
  - the latest user-visible turn,
  - any assistant-visible non-tool constraint text that precedes the latest user turn in the same request body,
- the latest user-visible turn must still be present and identifiable within that surface.

Positive scoring signals are then extracted from that effective prompt surface:

- length signal: the effective prompt surface is at least `classification.min_chars` Unicode characters or at least `classification.min_line_breaks` line breaks,
- reasoning signal: the effective prompt surface matches at least one reasoning pattern keyword such as `最少`, `保证`, `证明`, `抽屉`, `逻辑`, `组合`, `数学`, `pigeonhole`, `logic`, `minimum`, `guarantee`,
- output-constraint signal: the effective prompt surface matches at least one output-constraint pattern such as `只输出`, `only output`, `不要解释`, `输出一个数字`.

Matching semantics:

- `classification.reasoning_keyword_patterns`, `classification.output_constraint_patterns`, and `classification.code_marker_patterns` are regular expressions evaluated case-insensitively against the flattened effective prompt surface,
- a pattern counts as matched if any regex in the configured list matches at least once,
- requests with invalid regex configuration must fail proxy startup rather than degrade silently.

If classification confidence is low, the request should default to passthrough plus logging.

Hard gates:

- if the request is not replay-safe, passthrough,
- if the effective prompt surface contains repo/code markers, passthrough.

Scoring policy:

- start at score `0`,
- add `1` for flattenable latest user turn,
- add `1` if the effective prompt surface length is at least `classification.min_chars` Unicode characters or at least `classification.min_line_breaks` line breaks,
- add `1` if the effective prompt surface matches at least one reasoning pattern keyword such as `最少`, `保证`, `证明`, `抽屉`, `逻辑`, `组合`, `数学`, `pigeonhole`, `logic`, `minimum`, `guarantee`,
- add `1` if the effective prompt surface matches at least one output-constraint pattern such as `只输出`, `only output`, `不要解释`, `输出一个数字`.

Classification threshold:

- score `>= classification.rewrite_score_threshold`: reasoning-heavy, rewrite rules may apply,
- score `classification.normalize_only_score_threshold` through `classification.rewrite_score_threshold - 1`: log and normalize only,
- score `<= 1`: passthrough.

Configuration binding:

- `classification.min_chars` supplies the length threshold currently illustrated as `120`,
- `classification.min_line_breaks` supplies the line-break threshold currently illustrated as `2`,
- `classification.rewrite_score_threshold` supplies the rewrite gate threshold currently illustrated as `4`,
- `classification.normalize_only_score_threshold` supplies the lower bound of the normalize-only band currently illustrated as `2`,
- the numeric values shown in prose are defaults, not hardcoded constants; implementation must read the configured values,
- startup validation must reject configurations where `classification.min_chars < 1`, `classification.min_line_breaks < 0`, `classification.normalize_only_score_threshold < 0`, or `classification.rewrite_score_threshold < classification.normalize_only_score_threshold`,
- startup validation must also reject configurations where the normalize-only band is empty by mistake, unless `classification.rewrite_score_threshold = classification.normalize_only_score_threshold` is explicitly documented as collapsing normalize-only and rewrite onto the same score boundary.

Important boundary:

- failing the rewrite gate does not mean the request is excluded from diagnosis,
- all Claude Code traffic remains eligible for recording and replay comparison even when semantic rewrites are disabled for that request.

## Streaming Behavior

Phase 1 uses request-time rewriting and response-time streaming passthrough.

- The proxy may inspect and rewrite the full inbound request before anything is sent upstream.
- Once the upstream request starts, the proxy streams the upstream response downstream without semantic buffering or answer rewriting.
- The proxy may buffer up to the first complete SSE event, with a soft cap of 4096 bytes, only for an early framing sanity check.
- If no complete SSE event is available within 4096 buffered bytes, the proxy must stop framing validation, mark the stream as `framing_unverified` in recorder metadata, and begin passthrough immediately.
- `framing_unverified` is an expected degraded-observability state for large or unusual first events and must not by itself count as protocol failure.
- If a post-stream incompatibility is detected after downstream bytes have already been emitted, the proxy must terminate or propagate the stream failure in-place and record a compatibility fault; it must not attempt to synthesize a brand-new full Anthropic error body after the fact.

Rationale:

- phase 1 is focused on request-shape correction, not response synthesis,
- Claude Code depends on streaming for responsiveness and agent behavior,
- full-response buffering would blur the line between "preserve the agent" and "replace the agent."

Assumption to verify:

- the third-party provider correctly supports streaming Anthropic-compatible responses. This must be confirmed during implementation with a dedicated transport probe before enabling the proxy for normal Claude Code traffic.

## Error Handling

- Upstream transport errors return Anthropic-compatible error envelopes to the downstream client and are logged with local context.
- If a rewrite rule throws or produces invalid Anthropic payload shape, the proxy falls back to normalized passthrough.
- If response normalization fails, preserve the raw upstream body in recorder logs but still emit an Anthropic-compatible error envelope downstream.
- Logging failures must not block serving traffic.
- Upstream timeout before any bytes are returned: retry once if the request is still local-only, then fail fast with a logged timeout.
- Upstream timeout after downstream streaming has begun: abort and surface the upstream failure; do not retry because the client-visible stream is already in progress.
- Anthropic-incompatible upstream response schema: record the mismatch and emit an Anthropic-compatible compatibility error envelope downstream.

Definitions:

- "local-only" means no response bytes, SSE events, or HTTP body chunks have been emitted to the downstream client for that request attempt,
- any emitted downstream byte makes the request "partial" and therefore no longer retryable,
- retries for the same logical request must keep the same local `request_id` and append an `attempt` counter in recorder metadata so all attempts remain traceable under one request lineage.

Upstream error mapping:

- if the upstream already returns Anthropic-compatible error JSON, pass it through with original status code,
- if the upstream returns a non-Anthropic JSON error, wrap it into a local Anthropic-compatible error envelope while preserving the upstream status code and raw provider error body in recorder logs,
- if the upstream returns a non-JSON error body, surface a local compatibility error with the original status code and capture the raw body in recorder logs.

Downstream compatibility rule:

- raw upstream bodies are recorder artifacts only and must not be sent directly to Claude Code or Codex as the downstream response body,
- before any downstream bytes are emitted, the downstream client must see a valid Anthropic-compatible success shape or error shape,
- after downstream streaming has begun, protocol repair is best-effort only; the proxy may terminate the stream on incompatibility but must not emit a second synthetic full response body.

## Security and Secrets

- API tokens must be loaded from environment or config, not written into the spec-generated code by default.
- Recorder should redact `x-api-key`, `authorization`, `cookie`, GitHub PATs, and fields whose keys exactly match or suffix-match patterns such as `_token`, `_secret`, `_api_key`, `_password`, or `_credential` by default. Generic substrings like `key` alone must not trigger redaction.
- A configurable `logging.redaction_whitelist` may exempt exact field names from automatic redaction for local debugging, but auth headers themselves must never be whitelisted.
- Request logging should be easy to disable for sensitive sessions.

## Verification Plan

### Test Question

Use the candy-shape problem as the first hard acceptance test. The expected answer is `21`.

Canonical test prompt:

```text
在一个黑色的袋子里放有三种口味的糖果，每种糖果有两种不同的形状（圆形和五角星形，不同的形状靠手感可以分辨）。现已知不同口味的糖和不同形状的数量统计如下表。参赛者需要在活动前决定摸出的糖果数目，那么，最少取出多少个糖果才能保证手中同时拥有不同形状的苹果味和桃子味的糖？（同时手中有圆形苹果味匹配五角星桃子味糖果，或者有圆形桃子味匹配五角星苹果味糖果都满足要求）
苹果味 桃子味 西瓜味
圆形 7 9 8
五角星形 7 6 4
```

Correctness note:

- the correct answer is `21`,
- the phrase "不同的形状靠手感可以分辨" is a binding part of the task, not optional context,
- the evaluator must interpret the task as: the participant may intentionally choose candy by shape during drawing, but may not intentionally choose by flavor,
- any answer derived from a fully blind-draw interpretation is incorrect for this prompt, even if that answer would be correct under a different problem statement.

Acceptance rationale for `21`:

- drawing `9` round candies guarantees at least one round apple or one round peach, because there are only `8` round watermelons,
- drawing `12` star candies guarantees at least one star apple and at least one star peach, because there are only `10` non-apple stars and `11` non-peach stars,
- therefore `9 + 12 = 21` guarantees one valid cross-shape apple-peach pair,
- the evaluator should treat exact output `21` as correct and any alternative numeric answer such as those produced under blind-draw reasoning as incorrect.
- evaluator success for this prompt is strict exact-match `21`; if the model ignores the shape-selection premise and returns any other number, the run is counted as incorrect even if the explanation appears partially reasonable.

### Repetition

Run at least 10 repeated calls per mode at `temperature=0`:

- direct upstream,
- captured Claude raw upstream replay,
- captured Claude passthrough replay,
- captured Claude rewrite replay,
- proxy passthrough,
- proxy with each rewrite rule enabled independently,
- proxy with the best combined rule set.

Determinism note:

- `temperature=0` is treated as a control setting, not a determinism guarantee. The evaluation should explicitly record if the upstream provider still varies outputs at `temperature=0`.

Minimum seed suite:

- the candy-shape anchor problem,
- at least two additional non-candy reasoning prompts that require multi-step constraint handling,
- at least one negative control prompt where no reasoning rewrite should be necessary,
- at least one captured real Claude Code request replay sample outside the candy domain.

Scoring dimensions must be reported separately:

- reasoning correctness: whether the extracted final answer is mathematically correct,
- format compliance: whether the surface output obeys the requested output format,
- protocol compatibility: whether the downstream shape stayed acceptable to the client.

The evaluator must not count a pure format repair as a reasoning improvement.

Target-question variants:

- exact-output variant: asks for the final answer in strict minimal format,
- unconstrained variant: asks for the answer with explanation allowed.

Reasoning improvement is credited only if:

- the exact-output variant yields `21`, and
- the unconstrained variant also semantically yields `21` after answer extraction.

Generalization rule:

- the candy-shape problem is the primary acceptance anchor, but phase 1 must also report whether the same rewrite set preserves or improves the rest of the minimum seed suite,
- a change that only fixes the candy-shape prompt while regressing or failing to preserve the other seed prompts is not sufficient evidence of a generalized integration improvement.

Regression accounting:

- "does not materially regress the rest of the minimum seed suite" means:
  - no protocol-compatibility failures may be newly introduced on any non-candy seed prompt,
  - aggregate reasoning-correctness on non-candy seed prompts must not drop by more than 10 percentage points relative to the captured or passthrough baseline used for comparison,
  - aggregate format-compliance on non-candy seed prompts must not drop by more than 10 percentage points relative to that same baseline,
  - any single non-candy seed prompt that regresses from stable correct behavior to unstable or consistently incorrect behavior counts as a material regression regardless of aggregate averages.

### Success Criteria

Phase 1 is considered successful if all of the following hold:

- the proxy identifies at least one concrete request-shape difference that correlates with better reasoning,
- the proxy improves the target question from unstable wrong answers to at least `9/10` exact outputs of `21` in one batch and repeats that result in a second confirmation batch,
- the same rewrite set improves or preserves correctness on both the exact-output and unconstrained target-question variants, so the gain cannot be explained by format obedience alone,
- the same rewrite set does not materially regress the rest of the minimum seed suite,
- on a small replay suite of captured real Claude Code requests, `captured_claude_passthrough` and `captured_claude_rewrite` remain Anthropic-compatible and do not regress protocol handling relative to `captured_claude_raw_upstream`,
- at least one improvement claim is supported by an apples-to-apples comparison between the same captured Claude Code raw request replayed upstream directly and replayed through the proxy,
- the improved path does not replace the agent or require multi-sample answer merging,
- the proxy still passes through ordinary Anthropic-compatible traffic without breaking protocol shape.

### Failure Criteria

Phase 1 is considered unsuccessful if:

- no observable request-shape factor explains the performance gap,
- the best phase 1 rewrite set cannot reach the success threshold above,
- improvements appear only on hand-written prompts and cannot be reproduced on captured Claude Code raw request replays,
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
  config.py             # local config schema, thresholds, and toggles
  recorder.py           # request/response logging
  normalize.py          # protocol cleanup
  rewrite.py            # minimal reasoning-oriented rewrites
  upstream.py           # upstream HTTP transport, streaming, timeout, and retry helpers
  evals.py              # repeated-run evaluation driver
  fixtures/
    candy_question.json
```

The proxy should start as a local-only script or service, not as a final packaged Claude Code plugin.

Example local config file:

```toml
[server]
host = "127.0.0.1"
port = 8787
request_log_dir = "logs/requests"

[upstream]
base_url = "https://maas-coding-api.cn-huabei-1.xf-yun.com/anthropic"
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
```

Example fixture shape:

```json
{
  "name": "candy_question",
  "expected_answer": "21",
  "prompt": "..."
}
```

## Configuration Schema

Phase 1 configuration should include the following fields:

```text
server.host: str
server.port: int
server.request_log_dir: str

upstream.base_url: str
upstream.api_key_env: str
upstream.timeout_seconds: float
upstream.connect_timeout_seconds: float
upstream.max_retries: int
upstream.retry_statuses: list[int]
upstream.stream_probe_on_startup: bool
upstream.allow_self_target: bool

logging.redact_secrets: bool
logging.allow_raw_payload_logging: bool
logging.unsafe_override_via_env: str
logging.redaction_whitelist: list[str]

rewrite.enabled: bool
rewrite.max_tokens_floor.enabled: bool
rewrite.max_tokens_floor.minimum_output_tokens: int

rewrite.explicit_thinking.enabled: bool
rewrite.explicit_thinking.inject_when_missing: bool
rewrite.explicit_thinking.minimum_budget_tokens: int

rewrite.message_canonicalization.enabled: bool

rewrite.strict_format_guardrail.enabled: bool
rewrite.strict_format_guardrail.max_suffix_chars: int

rewrite.system_compression.enabled: bool
rewrite.system_compression.max_input_system_chars: int
rewrite.system_compression.target_system_chars: int

classification.enabled: bool
classification.min_chars: int
classification.min_line_breaks: int
classification.reasoning_keyword_patterns: list[str]
classification.output_constraint_patterns: list[str]
classification.code_marker_patterns: list[str]
classification.rewrite_score_threshold: int
classification.normalize_only_score_threshold: int
```

Default safety policy:

- `system_compression.enabled = false`,
- `allow_raw_payload_logging = false`,
- `upstream.max_retries = 1`,
- `upstream.allow_self_target = false`,
- `upstream.timeout_seconds = 60.0`,
- `upstream.connect_timeout_seconds = 10.0`,
- timeout values must be greater than `0.0` and less than or equal to `600.0`,
- `server.port` must be between `1` and `65535`,
- `upstream.base_url` must be an absolute `http` or `https` URL,
- `upstream.retry_statuses` entries must be valid HTTP status codes in the `4xx` or `5xx` ranges,
- `rewrite.enabled = true` only for explicit local experiments.

Unsafe override policy:

- raw unredacted payload logging may be enabled only by setting both `logging.allow_raw_payload_logging = true` and an environment variable named by `logging.unsafe_override_via_env` to `YES_I_ACCEPT_RAW_LOGGING_RISK`,
- if either condition is missing, the proxy must keep redaction enabled.

## Concurrency, Rate Limits, and Rollback

- Phase 1 assumes single-user local usage, but the listener must handle overlapping local requests without sharing mutable rewrite state between them.
- The proxy must not maintain global per-request scratch state outside request-local context and append-only logs.
- If the upstream provider enforces rate limits, the proxy should surface the upstream error directly and log the classification and enabled rewrite set for later analysis.
- Immediate proxy-local rollback path: set `rewrite.enabled = false` and reload or restart the proxy so semantic rewrites stop without requiring downstream client reconfiguration.
- Client-side fallback path: point `ANTHROPIC_BASE_URL` back to the upstream provider when you want to bypass the proxy entirely; this is operationally valid but depends on downstream client reconfiguration and is not an immediate proxy-controlled rollback.
- recorder storage must use size-bounded retention or file-count rotation so the proxy does not grow logs without bound,
- if the recorder cannot write because of disk-full or retention failure, serving traffic should continue in degraded mode with a one-time error marker in process logs and request metadata.

## Health, Shutdown, and Observability

- expose `GET /health` for process liveness,
- expose `GET /ready` for readiness, including a cached result of the upstream stream probe when enabled,
- use structured JSON logs for request lifecycle, rewrite decisions, retries, and fallback events,
- minimum metrics for phase 1: request latency, upstream latency, rewrite hit rate, passthrough rate, fallback rate, upstream error rate,
- on `SIGTERM`, stop accepting new requests, allow in-flight local requests a short grace period to finish streaming, then flush recorder buffers and exit.

## Risks and Mitigations

- Thinking injection may be ignored or misinterpreted by the upstream provider.
  Detection: compare passthrough vs thinking-enabled responses and inspect response schema.
- System compression may remove critical agent instructions.
  Mitigation: off by default, measured only after lighter rewrites are evaluated.
- Streaming compatibility may be imperfect on the third-party provider.
  Detection: run a startup stream probe and keep a passthrough-only mode.
- The provider may remain nondeterministic at `temperature=0`.
  Detection: repeated-run evaluator records output variance explicitly.

## Terminology

- "downstream client": Claude Code CLI, Codex, or a local test harness sending Anthropic-compatible requests to the proxy,
- "proxy inbound request": the HTTP request received by the proxy from the downstream client,
- "third-party upstream provider": the actual remote Anthropic-compatible service the proxy calls,
- "passthrough": forward normalized traffic without applying semantic rewrites,
- "normalized passthrough": passthrough after protocol-shape cleanup only.

## Open Questions Resolved In This Design

- The proxy may rewrite request content but must not change protocol shape.
- The first goal is diagnosis and minimal correction, not a production-ready plugin.
- Single-turn reasoning correctness is prioritized over speed.
- Multi-round or multi-sample answer merging is explicitly postponed.
- Claude Code and Codex are both in scope as clients, but the design is centered on preserving their existing agent capabilities rather than replacing them.

## Performance Targets

- target proxy-added request-time overhead before first upstream byte: under `100ms` on a typical local development machine when no expensive rewrite is enabled,
- target steady-state memory growth: bounded by request concurrency plus recorder buffers, with no unbounded accumulation across requests,
- these are local experiment targets, not production SLAs, but regressions beyond them should be treated as design failures for phase 1.

## Immediate Next Step

Write an implementation plan for the minimal proxy and evaluation harness, then build the logging-only path first before enabling any rewrite rules.

Example startup command:

```bash
python -m proxy.app --config ./proxy/config.toml
```
