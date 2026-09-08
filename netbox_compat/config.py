"""Загрузка и валидация compatibility.yaml.

Валидация выполняется до запуска любой стадии: ошибка в конфиге должна стоить
секунды, а не получаса прогона. Сообщения указывают путь до поля в конфиге,
чтобы их можно было читать прямо из лога CI.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

MODULE_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

ROOT_KEYS = ("netbox", "plugins")
NETBOX_KEYS = ("repository", "ref")
PLUGIN_KEYS = ("name", "repository", "ref", "package", "plugins_config")
PLUGIN_REQUIRED_KEYS = ("name", "repository", "ref", "package")


class ConfigError(Exception):
    """Конфиг непригоден к использованию; текст показывается пользователю."""


@dataclass(frozen=True)
class NetBoxSpec:
    repository: str
    ref: str


@dataclass(frozen=True)
class PluginSpec:
    name: str
    repository: str
    ref: str
    package: str
    plugins_config: dict[str, Any] = field(default_factory=dict)

    @property
    def pip_spec(self) -> str:
        """Аргумент для pip install."""
        return f"git+{self.repository}@{self.ref}"


@dataclass(frozen=True)
class CompatConfig:
    path: Path
    netbox: NetBoxSpec
    plugins: tuple[PluginSpec, ...]


def _type_name(value: Any) -> str:
    return {
        dict: "mapping",
        list: "list",
        str: "string",
        int: "integer",
        float: "float",
        bool: "boolean",
        type(None): "null",
    }.get(type(value), type(value).__name__)


def _require_mapping(value: Any, where: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ConfigError(f"{where}: expected mapping, got {_type_name(value)}")
    return value


def _require_string(mapping: dict[str, Any], key: str, where: str) -> str:
    if key not in mapping:
        raise ConfigError(f"{where}.{key}: missing required key")
    value = mapping[key]
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(
            f"{where}.{key}: expected non-empty string, got {_type_name(value)}"
        )
    return value.strip()


def _reject_unknown_keys(mapping: dict[str, Any], allowed: tuple[str, ...], where: str) -> None:
    for key in mapping:
        if key in allowed:
            continue
        suggestion = difflib.get_close_matches(str(key), allowed, n=1)
        hint = f" (did you mean {suggestion[0]!r}?)" if suggestion else ""
        raise ConfigError(f"{where}.{key}: unknown key{hint}")


def _parse_netbox(raw: Any) -> NetBoxSpec:
    mapping = _require_mapping(raw, "netbox")
    _reject_unknown_keys(mapping, NETBOX_KEYS, "netbox")
    return NetBoxSpec(
        repository=_require_string(mapping, "repository", "netbox"),
        ref=_require_string(mapping, "ref", "netbox"),
    )


def _parse_plugin(raw: Any, index: int) -> PluginSpec:
    where = f"plugins[{index}]"
    mapping = _require_mapping(raw, where)
    _reject_unknown_keys(mapping, PLUGIN_KEYS, where)
    for key in PLUGIN_REQUIRED_KEYS:
        _require_string(mapping, key, where)

    package = mapping["package"].strip()
    if not MODULE_NAME_RE.match(package):
        raise ConfigError(
            f"{where}.package: {package!r} is not a valid Python module name "
            f"(PLUGINS entries are module names, e.g. 'netbox_bgp')"
        )

    plugins_config = mapping.get("plugins_config", {})
    if plugins_config is None:
        plugins_config = {}
    _require_mapping(plugins_config, f"{where}.plugins_config")

    return PluginSpec(
        name=mapping["name"].strip(),
        repository=mapping["repository"].strip(),
        ref=mapping["ref"].strip(),
        package=package,
        plugins_config=plugins_config,
    )


def _parse_plugins(raw: Any) -> tuple[PluginSpec, ...]:
    if not isinstance(raw, list) or not raw:
        raise ConfigError(
            f"plugins: expected non-empty list, got {_type_name(raw)}"
        )

    plugins: list[PluginSpec] = []
    seen: dict[str, int] = {}
    for index, item in enumerate(raw):
        plugin = _parse_plugin(item, index)
        if plugin.package in seen:
            raise ConfigError(
                f"plugins[{index}].package: duplicate {plugin.package!r} "
                f"(also plugins[{seen[plugin.package]}])"
            )
        seen[plugin.package] = index
        plugins.append(plugin)
    return tuple(plugins)


def load_config(path: str | Path) -> CompatConfig:
    """Прочитать и провалидировать конфиг. Бросает ConfigError."""
    config_path = Path(path)
    try:
        text = config_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"{config_path}: cannot be read: {exc}") from exc

    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigError(f"{config_path}: invalid YAML: {exc}") from exc

    if raw is None:
        raise ConfigError(f"{config_path}: file is empty")
    mapping = _require_mapping(raw, "config root")
    _reject_unknown_keys(mapping, ROOT_KEYS, "config root")
    for key in ROOT_KEYS:
        if key not in mapping:
            raise ConfigError(f"{key}: missing required key")

    return CompatConfig(
        path=config_path,
        netbox=_parse_netbox(mapping["netbox"]),
        plugins=_parse_plugins(mapping["plugins"]),
    )
