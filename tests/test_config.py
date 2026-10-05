from pathlib import Path

import pytest

from trendlab.config.loader import ConfigError, load_config, update_global_config
from trendlab.config.schema import PermissionMode


def test_defaults_when_no_files(project: Path):
    cfg = load_config(project)
    assert cfg.remote_approval.enabled is False  # disabled until configured
    assert cfg.notifications.enabled is False
    assert cfg.defaults.permission_mode == PermissionMode.ASK
    assert cfg.remote_approval.request_timeout_minutes == 30


def test_project_overrides_global(project: Path, _trendlab_home: Path):
    _trendlab_home.mkdir(parents=True)
    (_trendlab_home / "config.toml").write_text(
        '[defaults]\nmodel = "openai:gpt-x"\npermission_mode = "ask"\n'
        "[remote_approval]\nenabled = true\nport = 9000\n"
    )
    (project / ".trendlab").mkdir()
    (project / ".trendlab" / "config.toml").write_text(
        '[defaults]\npermission_mode = "auto_edit"\n'
    )
    cfg = load_config(project)
    assert cfg.defaults.model == "openai:gpt-x"
    assert cfg.defaults.permission_mode == PermissionMode.AUTO_EDIT
    assert cfg.remote_approval.enabled and cfg.remote_approval.port == 9000


def test_invalid_toml_is_a_useful_error(_trendlab_home: Path):
    _trendlab_home.mkdir(parents=True)
    (_trendlab_home / "config.toml").write_text("[remote_approval\nenabled = ")
    with pytest.raises(ConfigError, match="invalid TOML"):
        load_config()


def test_invalid_values_rejected(_trendlab_home: Path):
    _trendlab_home.mkdir(parents=True)
    (_trendlab_home / "config.toml").write_text('[notifications]\nprovider = "pigeon"\n')
    with pytest.raises(ConfigError, match="pigeon"):
        load_config()


def test_update_global_config_roundtrip(_trendlab_home: Path):
    path = update_global_config("remote_approval", {"enabled": True, "port": 8790})
    assert path == _trendlab_home / "config.toml"
    cfg = load_config()
    assert cfg.remote_approval.enabled and cfg.remote_approval.port == 8790
    update_global_config("remote_approval", {"enabled": False})
    assert load_config().remote_approval.enabled is False
    assert load_config().remote_approval.port == 8790  # merge, not replace


def test_update_refuses_invalid(_trendlab_home: Path):
    with pytest.raises(ConfigError):
        update_global_config("remote_approval", {"channel": "carrier-pigeon"})
    assert not (_trendlab_home / "config.toml").exists()
