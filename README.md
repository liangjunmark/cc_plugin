# CC Proxy

Local Anthropic-compatible proxy and replay harness for diagnosing third-party reasoning regressions in Claude Code or Codex flows.

## Start

By default the factory loads `proxy/config.toml.example`. Override it with `CC_PROXY_CONFIG` if you want a local copy.

```bash
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m uvicorn proxy.app:create_app --factory --host 127.0.0.1 --port 8787
```

If `127.0.0.1:8787` is already in use, start the same command on another local port.

## Safety

- Keep `.claude/` out of git.
- Start with `rewrite.enabled = false`.
- Enable one rewrite rule at a time.
- Use the recorder artifacts to compare raw replay, passthrough, and rewrite modes before claiming improvement.

## Evaluation Workflow

Use the seed fixtures in `proxy/fixtures/` to benchmark direct and proxy-oriented modes with the same prompt payload.

- `candy_question.json` is the acceptance fixture; expected answer is strict exact-match `21`.
- `non_candy_format.json` is a control for exact-output behavior.
- `non_candy_reasoning.json` is a control for non-format-constrained reasoning.
- `non_candy_exact_output_suite.json` is a small exact-output reasoning suite for measuring how narrow the current xfyun-only `phase2b.boundary_verifier` uplift really is.

Start with `run_mode("direct", fixture, config)` and compare it against the proxy-targeted modes before enabling additional rewrites.

To run the non-candy exact-output suite in one batch:

```bash
CC_PROXY_CONFIG=/path/to/phase2b-config.toml \
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m proxy.evals \
  --mode claude_code_proxy_phase2b \
  --fixture-set proxy/fixtures/non_candy_exact_output_suite.json \
  --repeat 3
```

## Phase 2

Phase 2 is a non-streamed experimental path layered on top of phase 1.

- `claude_code_proxy_phase1` sends `x-cc-proxy-phase: phase1`; `claude_code_proxy_phase2` sends `x-cc-proxy-phase: phase2`.
- The proxy consumes the selector and does not forward it upstream.
- `stream=true` requests stay on phase 1.
- Phase 2 keeps one retained phase-1 baseline response for fallback.
- Phase 2 runs two structured candidate solves and at most one adjudication call.
- Compare `claude_code_proxy_phase1` and `claude_code_proxy_phase2` on the same fixtures before claiming improvement.

## Real Provider Validation

1. Start the proxy with `phase2.enabled = true` and `phase2.allow_streaming_requests = false`.
2. Run `direct`, `claude_code_proxy_phase1`, and `claude_code_proxy_phase2` on `proxy/fixtures/candy_question.json`.
3. Repeat the same batch on at least one non-candy reasoning fixture.
4. Record correctness, format compliance, protocol compatibility, latency, upstream call count, and token usage if available.

Protocol compatibility is scored independently of answer correctness and output format. A successful evaluation response must be a 2xx Anthropic message envelope with a `content` list; a non-2xx response must use an Anthropic error envelope with `type: "error"` and typed error message fields. Plaintext failures and unrelated JSON error shapes are protocol failures.

## Phase 2b

Phase 2b is a narrow experimental path for single-turn exact-output reasoning prompts.

- It keeps one retained phase-1 baseline.
- It runs branch generation plus attack rounds before the final answer.
- It is intentionally high-cost in the first implementation.
- The first success target is the candy fixture reaching exact-match `21` in repeated runs.

### Acceptance Batch

Before running the batch:

1. Create or select a config with `phase2b.enabled = true`.
2. Configure valid upstream credentials in that config's `upstream.api_key_env` environment variable.
3. Set `CC_PROXY_CONFIG` to that config so the eval client and the proxy use the intended host and port.
4. Start the proxy listener on the matching configured host and port.

For example, start the configured listener before the batch:

```bash
CC_PROXY_CONFIG=/path/to/phase2b-config.toml \
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m uvicorn proxy.app:create_app --factory \
  --host 127.0.0.1 --port 8787
```

```bash
CC_PROXY_CONFIG=/path/to/phase2b-config.toml \
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m proxy.evals \
  --mode claude_code_proxy_phase2b \
  --fixture proxy/fixtures/candy_question.json \
  --repeat 10
```
