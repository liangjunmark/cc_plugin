### Task 7: End-To-End Verification And Operator Notes

**Files:**
- Modify: `proxy/app.py`
- Modify: `proxy/config.toml.example`
- Create: `README.md`

**Interfaces:**
- Consumes: all prior tasks
- Produces: local startup command and evaluation workflow

**Task Notes:**
- `uvicorn proxy.app:create_app --factory` must work with no positional config argument.
- Keep the startup path compatible with the example config while allowing local port overrides.
- Document the safe rollout sequence before enabling semantic rewrites.
