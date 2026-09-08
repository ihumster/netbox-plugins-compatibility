"""Источник версий-кандидатов для режима --resolve.

Границы совместимости плагин объявляет в PluginConfig, и NetBox сам отвергает
плагин, вышедший за них (settings.py: warnings.warn + continue). Поэтому отсев
по объявленным границам — не эвристика, а точный фильтр: такая версия не
сможет пройти стадию startup. Читаем их из git-ref'а статически, через
blobless-клон и ast, ничего не устанавливая и не импортируя.
"""

from __future__ import annotations

import ast
import logging
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from packaging.version import InvalidVersion, Version

from .config import PluginSpec
from .environment import slugify

log = logging.getLogger("netbox_compat")

# Теги в экосистеме NetBox пишут по-разному: v0.19.0, v.0.5.1, 1.7.2.
TAG_PREFIX_RE = re.compile(r"^v\.?")

# Стандартные раскладки пакета плагина в репозитории.
PACKAGE_PATHS = ("{package}/__init__.py", "src/{package}/__init__.py")


@dataclass(frozen=True)
class Candidate:
    """Тег плагина, рассмотренный на пригодность к целевой версии NetBox."""

    tag: str
    version: Version
    min_version: str | None
    max_version: str | None
    eligible: bool
    reason: str = ""

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {"tag": self.tag}
        if self.min_version:
            payload["min_version"] = self.min_version
        if self.max_version:
            payload["max_version"] = self.max_version
        if self.reason:
            payload["reason"] = self.reason
        return payload


def parse_tag(tag: str) -> Version | None:
    try:
        return Version(TAG_PREFIX_RE.sub("", tag))
    except InvalidVersion:
        return None


def parse_bounds(source: str) -> dict[str, str]:
    """Достать min_version/max_version из PluginConfig, не исполняя модуль."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return {}

    bounds: dict[str, str] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        for statement in node.body:
            if not isinstance(statement, ast.Assign) or not isinstance(statement.value, ast.Constant):
                continue
            if not isinstance(statement.value.value, str):
                continue
            for target in statement.targets:
                if isinstance(target, ast.Name) and target.id in ("min_version", "max_version"):
                    bounds[target.id] = statement.value.value
    return bounds


def _evaluate(tag: str, version: Version, bounds: dict[str, str], netbox: Version) -> Candidate:
    minimum = bounds.get("min_version")
    maximum = bounds.get("max_version")
    common = {"tag": tag, "version": version, "min_version": minimum, "max_version": maximum}

    if minimum:
        try:
            if netbox < Version(minimum):
                return Candidate(**common, eligible=False, reason=f"declares min_version {minimum}")
        except InvalidVersion:
            pass
    if maximum:
        try:
            if netbox > Version(maximum):
                return Candidate(**common, eligible=False, reason=f"declares max_version {maximum}")
        except InvalidVersion:
            pass
    return Candidate(**common, eligible=True)


class TagSource:
    """Blobless-клон репозитория плагина: теги и один файл из любого ref'а."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self._clones: dict[str, Path] = {}

    def _clone(self, repository: str) -> Path:
        if repository in self._clones:
            return self._clones[repository]
        path = self.root / slugify(repository.rsplit("/", 1)[-1].removesuffix(".git"))
        if not (path / "HEAD").exists() and not (path / ".git").exists():
            subprocess.run(
                ["git", "clone", "--quiet", "--filter=blob:none", "--no-checkout", repository, str(path)],
                check=True,
                capture_output=True,
                text=True,
            )
        else:
            subprocess.run(
                ["git", "-C", str(path), "fetch", "--quiet", "--tags", "--prune"],
                check=False,
                capture_output=True,
                text=True,
            )
        self._clones[repository] = path
        return path

    def _show(self, clone: Path, ref: str, path: str) -> str | None:
        result = subprocess.run(
            ["git", "-C", str(clone), "show", f"{ref}:{path}"],
            capture_output=True,
            text=True,
        )
        return result.stdout if result.returncode == 0 else None

    def read_plugin_init(self, clone: Path, ref: str, package: str) -> str | None:
        for template in PACKAGE_PATHS:
            source = self._show(clone, ref, template.format(package=package))
            if source is not None:
                return source
        return None

    def candidates(
        self,
        plugin: PluginSpec,
        netbox_version: Version | None,
        *,
        include_prereleases: bool = False,
    ) -> tuple[list[Candidate], list[Candidate]]:
        """Вернуть (пригодные, отклонённые), обе — от новых версий к старым."""
        clone = self._clone(plugin.repository)
        tags = subprocess.run(
            ["git", "-C", str(clone), "tag", "--list"], capture_output=True, text=True, check=True
        ).stdout.split()

        parsed: dict[Version, str] = {}
        for tag in tags:
            version = parse_tag(tag)
            if version is None:
                continue
            if version.is_prerelease and not include_prereleases:
                continue
            # Один и тот же выпуск может быть под двумя тегами (v0.5.0 и v.0.5.0).
            parsed.setdefault(version, tag)

        eligible: list[Candidate] = []
        rejected: list[Candidate] = []
        for version in sorted(parsed, reverse=True):
            tag = parsed[version]
            source = self.read_plugin_init(clone, tag, plugin.package)
            if source is None:
                rejected.append(
                    Candidate(tag=tag, version=version, min_version=None, max_version=None,
                              eligible=False, reason=f"no {plugin.package}/__init__.py at this tag")
                )
                continue

            bounds = parse_bounds(source)
            if netbox_version is None:
                # Версию NetBox определить не удалось — статически не отсеиваем.
                eligible.append(
                    Candidate(tag=tag, version=version, min_version=bounds.get("min_version"),
                              max_version=bounds.get("max_version"), eligible=True)
                )
                continue

            candidate = _evaluate(tag, version, bounds, netbox_version)
            (eligible if candidate.eligible else rejected).append(candidate)

        return eligible, rejected
