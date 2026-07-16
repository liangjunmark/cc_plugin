from pathlib import Path

import pytest

from proxy.config import load_config


@pytest.fixture
def config() -> object:
    return load_config(Path("proxy/config.toml.example"))
