import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.run_dispatch import _mcp_server_specs


def _env(env_dir: Path, command=None):
    return SimpleNamespace(
        name="generated-relocatable-env-v1",
        env_dir=env_dir,
        meta={
            "entrypoints": {
                "mcp": {
                    "enabled": True,
                    "transport": "stdio",
                    "name": "octagon-generated-relocatable-env-v1",
                    "command": command or ["python", "mcp_server.py"],
                }
            }
        },
    )


def test_mcp_command_is_resolved_from_loaded_environment_directory(tmp_path):
    env_dir = tmp_path / "arbitrary-mount" / "bundle"
    env_dir.mkdir(parents=True)
    (env_dir / "mcp_server.py").write_text("print('ready')\n", encoding="utf-8")
    spec = _mcp_server_specs(_env(env_dir))[0]
    assert spec.command == sys.executable
    assert spec.args == ("mcp_server.py",)
    assert spec.cwd == str(env_dir.resolve())
    assert (Path(spec.cwd) / spec.args[-1]).is_file()


def test_mcp_spec_does_not_require_envs_name_install_layout(tmp_path):
    env_dir = tmp_path / "run" / "candidate-123" / "bundle"
    env_dir.mkdir(parents=True)
    spec = _mcp_server_specs(_env(env_dir))[0]
    assert spec.cwd == str(env_dir.resolve())
    assert "/envs/generated-relocatable-env-v1" not in " ".join(spec.args)


def test_mcp_command_shape_remains_fail_closed(tmp_path):
    with pytest.raises(ValueError, match="command"):
        _mcp_server_specs(_env(tmp_path, command=[]))
