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

Start with `run_mode("direct", fixture, config)` and compare it against the proxy-targeted modes before enabling additional rewrites.
