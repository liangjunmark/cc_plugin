from __future__ import annotations

from pathlib import Path
import subprocess


def test_claude_code_config_example_loads() -> None:
    from proxy.config import load_config, validate_runtime_config

    config = load_config(Path("proxy/config.claude-code.toml.example"))
    validate_runtime_config(config)

    assert config.server.host == "127.0.0.1"
    assert config.server.port == 8787
    assert config.upstream.api_key_env == "ANTHROPIC_AUTH_TOKEN"


def test_claude_code_env_script_prints_proxy_exports() -> None:
    result = subprocess.run(
        ["bash", "scripts/claude-code-env.sh", "127.0.0.1", "8787"],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    assert 'export ANTHROPIC_BASE_URL="http://127.0.0.1:8787"' in result.stdout
    assert "export ANTHROPIC_AUTH_TOKEN=" in result.stdout
    assert "export ANTHROPIC_MODEL=" in result.stdout


def test_start_claude_code_proxy_script_has_valid_bash_syntax() -> None:
    result = subprocess.run(
        ["bash", "-n", "scripts/start-claude-code-proxy.sh"],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
