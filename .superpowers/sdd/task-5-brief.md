### Task 5: Build FastAPI Listener With Anthropic-Compatible Streaming

**Files:**
- Create: `proxy/app.py`
- Test: `tests/test_app.py`

**Interfaces:**
- Consumes: `ProxyConfig`, `Recorder`, `UpstreamTransport`, `ClassificationResult`, `RewriteResult`
- Produces: `create_app(config: ProxyConfig) -> FastAPI`
- Produces: `POST /v1/messages`
- Produces: `GET /health`
- Produces: `GET /ready`

**Task Notes:**
- Preserve live upstream streaming once downstream bytes begin.
- Return an Anthropic-compatible error envelope whenever no downstream bytes have been emitted yet.
- Keep rewrite behavior gated behind `classify_request(...)` and `config.rewrite.enabled`.
- Keep the app testable by allowing transport injection in tests.
