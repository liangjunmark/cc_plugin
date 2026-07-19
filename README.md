# CC Proxy

Local Anthropic-compatible proxy and replay harness for diagnosing third-party reasoning regressions in Claude Code or Codex flows.

## Claude Code CLI

This repository is ready to sit in front of `Claude Code CLI` as a local Anthropic-compatible proxy. It is not a packaged Claude plugin; the integration path is:

1. Start the local proxy.
2. Point `ANTHROPIC_BASE_URL` at the local proxy.
3. Keep `ANTHROPIC_AUTH_TOKEN` and `ANTHROPIC_MODEL` set to the real upstream provider values so the proxy can forward them.

Minimal setup:

```bash
cp proxy/config.claude-code.toml.example /tmp/cc-proxy.toml
# edit /tmp/cc-proxy.toml and set upstream.base_url
export CC_PROXY_CONFIG=/tmp/cc-proxy.toml
./scripts/start-claude-code-proxy.sh
```

In the shell where you launch `Claude Code CLI`:

```bash
eval "$(./scripts/claude-code-env.sh 127.0.0.1 8787 /tmp/cc-proxy.toml)"
```

That sets:

- `CC_PROXY_CONFIG` for the local proxy process
- `ANTHROPIC_BASE_URL=http://127.0.0.1:8787` for Claude Code CLI
- passthrough `ANTHROPIC_AUTH_TOKEN` and `ANTHROPIC_MODEL` exports for the upstream provider

Current validated uplift is intentionally narrow:

- ordinary Claude Code CLI traffic can pass through the proxy,
- validated reasoning uplift currently targets a narrow reasoning subset,
- eligible `phase2b` Claude Code CLI `stream=true` requests now use an internal non-stream solve plus synthesized Anthropic SSE bridge,
- the most stable gains so far are the candy boundary family and guarantee-counting family handled by `phase2b`,
- when `phase2b` is used, the proxy now tries to preserve the user's requested output style instead of forcing answer-only output.

### Project-Local Setup

If you want `Claude Code CLI` to use the proxy only in this repository, keep everything under `.claude/` and do not touch your global shell profile.

1. Create a repo-local proxy config.

```bash
mkdir -p .claude
cp proxy/config.claude-code.toml.example .claude/cc-proxy.toml
```

2. Edit `.claude/cc-proxy.toml`.

- set `[upstream].base_url` to your Anthropic-compatible provider,
- keep `request_log_dir = "logs/requests"`,
- choose a free local port such as `8791`.

3. Create or update `.claude/settings.local.json` so Claude Code CLI points at the local proxy only for this repo.

Example:

```json
{
  "env": {
    "ANTHROPIC_AUTH_TOKEN": "<your-provider-token>",
    "ANTHROPIC_BASE_URL": "http://127.0.0.1:8791",
    "ANTHROPIC_MODEL": "<your-provider-model>",
    "ANTHROPIC_DEFAULT_FABLE_MODEL": "<your-provider-model>",
    "ANTHROPIC_DEFAULT_FABLE_MODEL_NAME": "<your-provider-model>",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL": "<your-provider-model>",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL_NAME": "<your-provider-model>",
    "ANTHROPIC_DEFAULT_OPUS_MODEL": "<your-provider-model>",
    "ANTHROPIC_DEFAULT_OPUS_MODEL_NAME": "<your-provider-model>",
    "ANTHROPIC_DEFAULT_SONNET_MODEL": "<your-provider-model>",
    "ANTHROPIC_DEFAULT_SONNET_MODEL_NAME": "<your-provider-model>",
    "CC_PROXY_CONFIG": "/abs/path/to/cc_plugin/.claude/cc-proxy.toml",
    "CC_PROXY_PORT": "8791"
  }
}
```

Notes:

- `.claude/` is gitignored in this repo, so project-local provider settings stay local.
- If you already have `permissions` or other local Claude settings, merge the `env` block instead of replacing the file.
- `settings.json` and `settings.local.json` are both repo-local; use either if your Claude Code setup prefers one, but keep the JSON valid because malformed files are skipped completely.

4. Start the proxy from the repo root.

```bash
CC_PROXY_CONFIG=/abs/path/to/cc_plugin/.claude/cc-proxy.toml \
CC_PROXY_PORT=8791 \
./scripts/start-claude-code-proxy.sh
```

5. Start `Claude Code CLI` from this repository and ask your test question. The CLI should use `http://127.0.0.1:8791` only for this repo.

### Project-Local Testing

Recommended smoke checks before opening Claude Code CLI:

```bash
curl -sf http://127.0.0.1:8791/health
curl -sf http://127.0.0.1:8791/ready
```

Then test from the repo root:

1. Restart `Claude Code CLI` after changing `.claude/settings*.json`.
2. Ask the target question directly in the CLI.
3. Inspect `logs/requests/<request-id>/metadata.json` and `attempt-*-request.json` if the answer is wrong or the proxy path is not being used.

Useful local checks while testing:

```bash
ss -ltnp | rg ':8791\\b'
find logs/requests -maxdepth 1 -mindepth 1 -type d | tail
tail -f logs/claude-code-proxy-local.log
```

What to expect:

- title-generation requests and main user requests are logged separately,
- eligible `phase2b` streamed Claude Code requests are internally solved with `stream = false` and returned to the CLI as Anthropic-compatible SSE,
- the current uplift is still narrow; a passing candy test does not imply broad reasoning improvement across all prompts.

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
- For eligible Claude Code CLI streamed requests, the proxy runs the `phase2b` solve on an internal `stream=false` copy and emits a synthetic Anthropic SSE success stream downstream.
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
