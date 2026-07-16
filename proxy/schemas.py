from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(slots=True)
class RequestContext:
    request_id: str
    attempt: int
    log_dir: Path
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class NormalizedRequest:
    body: dict[str, Any]
    forward_headers: dict[str, str]
    recorder_headers: dict[str, str]
