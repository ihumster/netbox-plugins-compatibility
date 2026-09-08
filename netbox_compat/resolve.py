"""Режим --resolve: поиск версии плагина, работающей на целевой версии NetBox.

Порядок перебора задаёт статический фильтр по объявленным границам, но вердикт
всегда даёт фактический прогон install/migrate/startup: объявленная
совместимость не гарантирует ни рабочих миграций, ни разрешимых зависимостей.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml
from packaging.version import InvalidVersion, Version

from .config import CompatConfig, NetBoxSpec, PluginSpec
from .environment import SourceCache
from .runner import MODE_RESOLVE, RunOptions, Target, run_target
from .stages import FAILED, PASSED
from .versions import TagSource, parse_tag

log = logging.getLogger("netbox_compat")

RELEASE_YAML = "netbox/release.yaml"

RESOLVED_HEADER = """# Сгенерировано `python -m netbox_compat --resolve`. Не редактируется вручную:
# перенесите нужные ref'ы в compatibility.yaml через PR.
#
# Прогон: {started}
# Целевой NetBox: {ref} ({version})
"""


@dataclass
class ResolveOptions:
    max_attempts: int
    include_prereleases: bool
    resolved_config: Path


def netbox_version_at(cache: SourceCache, netbox: NetBoxSpec, log_path: Path) -> Version | None:
    """Точная версия NetBox на ref'е: из netbox/release.yaml, а не из имени тега."""
    prepared = cache.ensure(netbox.repository, log_path)
    if prepared is not None and not prepared.ok:
        return None

    raw = cache.show(netbox.repository, netbox.ref, RELEASE_YAML)
    if raw:
        try:
            declared = (yaml.safe_load(raw) or {}).get("version")
            if declared:
                return Version(str(declared))
        except (yaml.YAMLError, InvalidVersion):
            pass
    # release.yaml появился не во всех версиях — падаем обратно на имя тега.
    return parse_tag(netbox.ref)


def resolve_plugin(
    plugin: PluginSpec,
    config: CompatConfig,
    options: RunOptions,
    resolve_options: ResolveOptions,
    cache: SourceCache,
    tags: TagSource,
    netbox_version: Version | None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Вернуть (итог по плагину, записи фактических прогонов)."""
    eligible, rejected = tags.candidates(
        plugin, netbox_version, include_prereleases=resolve_options.include_prereleases
    )
    log.info(
        "[%s] %d tag(s) eligible, %d rejected by declared bounds",
        plugin.package,
        len(eligible),
        len(rejected),
    )

    summary: dict[str, Any] = {
        "name": plugin.name,
        "package": plugin.package,
        "repository": plugin.repository,
        "configured_ref": plugin.ref,
        "resolved_ref": None,
        "status": "unresolved",
        "tags_eligible": len(eligible),
        "tags_rejected": len(rejected),
        # Отклонённые режут по объявленным границам — это точный отсев, а не
        # догадка: NetBox сам не загрузит такую версию.
        "rejected_by_declared_bounds": [c.to_dict() for c in rejected[: resolve_options.max_attempts]],
        "attempts": [],
    }

    if not eligible:
        summary["detail"] = (
            f"no tag declares compatibility with NetBox {netbox_version}"
            if netbox_version
            else "no usable tags found"
        )
        return summary, []

    records: list[dict[str, Any]] = []
    for candidate in eligible[: resolve_options.max_attempts]:
        target = Target(
            id=f"{plugin.package}@{candidate.tag}",
            mode=MODE_RESOLVE,
            plugins=(replace(plugin, ref=candidate.tag),),
        )
        log.info("[%s] trying %s", plugin.package, candidate.tag)
        record = run_target(target, config.netbox, options, cache)
        records.append(record)

        failed_stage = next((s for s in record["stages"] if s["status"] == FAILED), None)
        summary["attempts"].append(
            {
                "ref": candidate.tag,
                "status": record["status"],
                "target": target.id,
                "failed_stage": failed_stage["name"] if failed_stage else None,
                "detail": failed_stage.get("detail") if failed_stage else None,
            }
        )

        if record["status"] == PASSED:
            summary["resolved_ref"] = candidate.tag
            summary["status"] = "resolved"
            log.info("[%s] resolved to %s", plugin.package, candidate.tag)
            break
    else:
        summary["detail"] = (
            f"none of the {len(summary['attempts'])} attempted ref(s) passed "
            f"(--max-attempts {resolve_options.max_attempts})"
        )

    return summary, records


def render_resolved_config(
    config: CompatConfig, resolution: dict[str, Any], started: str
) -> str:
    """Матрица с найденными ref'ами, в форме исходного compatibility.yaml."""
    lines = [
        RESOLVED_HEADER.format(
            started=started,
            ref=config.netbox.ref,
            version=resolution.get("netbox_version") or "unknown",
        ),
        "netbox:",
        f"  repository: {config.netbox.repository}",
        f"  ref: {config.netbox.ref}",
        "",
        "plugins:",
    ]
    by_package = {entry["package"]: entry for entry in resolution["plugins"]}
    for plugin in config.plugins:
        entry = by_package[plugin.package]
        ref = entry["resolved_ref"] or plugin.ref
        lines.append(f"  - name: {plugin.name}")
        lines.append(f"    repository: {plugin.repository}")
        if entry["status"] == "resolved":
            note = "" if ref == plugin.ref else f"  # было {plugin.ref}"
            lines.append(f"    ref: {ref}{note}")
        else:
            lines.append(f"    # UNRESOLVED: {entry.get('detail', 'no working ref found')}")
            lines.append(f"    ref: {ref}")
        lines.append(f"    package: {plugin.package}")
        if plugin.plugins_config:
            lines.append("    plugins_config:")
            for key, value in plugin.plugins_config.items():
                lines.append(f"      {key}: {yaml.safe_dump(value, default_flow_style=True).strip()}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def run_resolve(
    config: CompatConfig, options: RunOptions, resolve_options: ResolveOptions
) -> dict[str, Any]:
    started_at = datetime.now(timezone.utc)
    started = time.monotonic()
    cache = SourceCache(options.workdir / "_cache")
    tags = TagSource(options.workdir / "_tags")

    version_log = options.output_dir / "logs" / "resolve.log"
    netbox_version = netbox_version_at(cache, config.netbox, version_log)
    if netbox_version is None:
        log.warning(
            "cannot determine the NetBox version at %s; every tag will be tried in order",
            config.netbox.ref,
        )
    else:
        log.info("resolving against NetBox %s (%s)", netbox_version, config.netbox.ref)

    summaries: list[dict[str, Any]] = []
    records: list[dict[str, Any]] = []
    for plugin in config.plugins:
        summary, plugin_records = resolve_plugin(
            plugin, config, options, resolve_options, cache, tags, netbox_version
        )
        summaries.append(summary)
        records.extend(plugin_records)

    resolved = sum(1 for entry in summaries if entry["status"] == "resolved")
    resolution = {
        "netbox_version": str(netbox_version) if netbox_version else None,
        "max_attempts": resolve_options.max_attempts,
        "include_prereleases": resolve_options.include_prereleases,
        "totals": {
            "plugins": len(summaries),
            "resolved": resolved,
            "unresolved": len(summaries) - resolved,
        },
        "plugins": summaries,
    }

    report = {
        "schema_version": 1,
        "config": str(config.path),
        "mode": MODE_RESOLVE,
        "netbox": {"repository": config.netbox.repository, "ref": config.netbox.ref},
        "status": PASSED if resolved == len(summaries) and summaries else FAILED,
        "started_at": started_at.isoformat(),
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "duration_seconds": round(time.monotonic() - started, 1),
        "totals": {
            "targets": len(records),
            "passed": sum(1 for r in records if r["status"] == PASSED),
            "failed": sum(1 for r in records if r["status"] == FAILED),
        },
        "resolution": resolution,
        "targets": records,
    }

    resolve_options.resolved_config.parent.mkdir(parents=True, exist_ok=True)
    resolve_options.resolved_config.write_text(
        render_resolved_config(config, resolution, started_at.isoformat()), encoding="utf-8"
    )
    log.info("resolved matrix: %s", resolve_options.resolved_config)
    return report
