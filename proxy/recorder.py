import json
from pathlib import Path
from typing import Any

from proxy.schemas import RequestContext


class Recorder:
    def __init__(self, root: Path) -> None:
        self.root = root

    def write_artifact(self, context: RequestContext, name: str, payload: dict | str | bytes) -> None:
        context.log_dir.mkdir(parents=True, exist_ok=True)
        path = context.log_dir / f"{name}.json"
        if isinstance(payload, bytes):
            path.write_bytes(payload)
            return
        if isinstance(payload, str):
            path.write_text(payload, encoding="utf-8")
            return
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
