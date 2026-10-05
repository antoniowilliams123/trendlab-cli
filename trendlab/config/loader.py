"""Load and merge TrendLab configuration.

Precedence (lowest → highest): built-in defaults → ``~/.trendlab/config.toml``
→ ``<project>/.trendlab/config.toml``. ``TRENDLAB_HOME`` overrides the global
directory (used by tests and portable installs).
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Any

import tomli_w
from pydantic import ValidationError

from trendlab.config.schema import AppConfig

GLOBAL_DIR_NAME = ".trendlab"
PROJECT_DIR_NAME = ".trendlab"
CONFIG_FILE_NAME = "config.toml"
PROJECT_INSTRUCTIONS_FILE = "TRENDLAB.md"


class ConfigError(Exception):
    """Raised when configuration cannot be loaded or is invalid."""


def trendlab_home() -> Path:
    override = os.environ.get("TRENDLAB_HOME")
    return Path(override).expanduser() if override else Path.home() / GLOBAL_DIR_NAME


def global_config_path() -> Path:
    return trendlab_home() / CONFIG_FILE_NAME


def project_config_path(project_root: Path) -> Path:
    return project_root / PROJECT_DIR_NAME / CONFIG_FILE_NAME


def _read_toml(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as fh:
            return tomllib.load(fh)
    except FileNotFoundError:
        return {}
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"invalid TOML in {path}: {exc}") from exc


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def load_config(project_root: Path | None = None) -> AppConfig:
    data = _read_toml(global_config_path())
    if project_root is not None:
        data = _deep_merge(data, _read_toml(project_config_path(project_root)))
    try:
        return AppConfig.model_validate(data)
    except ValidationError as exc:
        raise ConfigError(f"invalid configuration: {exc}") from exc


def update_global_config(section: str, values: dict[str, Any]) -> Path:
    """Merge ``values`` into ``[section]`` of the global config file and write it back."""
    return _update_config_file(global_config_path(), section, values)


def update_project_config(project_root: Path, section: str, values: dict[str, Any]) -> Path:
    """Merge ``values`` into ``[section]`` of ``<project>/.trendlab/config.toml``."""
    return _update_config_file(project_config_path(project_root), section, values)


def _update_config_file(path: Path, section: str, values: dict[str, Any]) -> Path:
    data = _read_toml(path)
    data[section] = _deep_merge(data.get(section, {}), values)
    # Validate the merged result before persisting.
    try:
        AppConfig.model_validate(data)
    except ValidationError as exc:
        raise ConfigError(f"refusing to write invalid configuration: {exc}") from exc
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as fh:
        tomli_w.dump(data, fh)
    return path
