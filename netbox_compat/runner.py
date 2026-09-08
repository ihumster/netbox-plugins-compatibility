"""Оркестрация: цели тестирования, последовательные стадии, изоляция провалов."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import CompatConfig, PluginSpec
from .environment import Backends, SourceCache, TargetEnvironment
from .process import clean_env, run_logged
from .stages import (
    FAILED,
    PASSED,
    SKIPPED,
    STAGE_ORDER,
    StageResult,
    stage_install,
    stage_migrate,
    stage_startup,
)

log = logging.getLogger("netbox_compat")

MODE_ISOLATED = "isolated"
MODE_COMBINED = "combined"
MODE_BOTH = "both"
MODES = (MODE_ISOLATED, MODE_COMBINED, MODE_BOTH)

COMBINED_TARGET_ID = "all-plugins"


@dataclass(frozen=True)
class Target:
    """Объект тестирования: один инстанс NetBox и набор плагинов в нём."""

    id: str
    mode: str
    plugins: tuple[PluginSpec, ...]


def build_targets(config: CompatConfig, mode: str) -> list[Target]:
    targets: list[Target] = []
    if mode in (MODE_ISOLATED, MODE_BOTH):
        targets += [
            Target(id=plugin.package, mode=MODE_ISOLATED, plugins=(plugin,))
            for plugin in config.plugins
        ]
    if mode in (MODE_COMBINED, MODE_BOTH):
        targets.append(Target(id=COMBINED_TARGET_ID, mode=MODE_COMBINED, plugins=config.plugins))
    return targets


@dataclass
class RunOptions:
    workdir: Path
    output_dir: Path
    python: str
    mode: str
    backends: Backends
    startup_timeout: float
    keep_workdir: bool


def _stage_to_dict(stage: StageResult, output_dir: Path) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "name": stage.name,
        "status": stage.status,
        "returncode": stage.returncode,
        "duration_seconds": round(stage.duration, 1),
    }
    if stage.log:
        log_path = Path(stage.log)
        try:
            payload["log"] = str(log_path.relative_to(output_dir))
        except ValueError:
            payload["log"] = str(log_path)
    if stage.detail:
        payload["detail"] = stage.detail
    if stage.status == FAILED:
        if stage.stdout_tail:
            payload["stdout_tail"] = stage.stdout_tail
        if stage.stderr_tail:
            payload["stderr_tail"] = stage.stderr_tail
    return payload


def run_target(target: Target, config: CompatConfig, options: RunOptions, cache: SourceCache) -> dict[str, Any]:
    """Прогнать одну цель. Исключения гасятся здесь: пайплайн не должен падать."""
    started_at = datetime.now(timezone.utc)
    started = time.monotonic()
    env = TargetEnvironment(
        target_id=target.id,
        root=options.workdir / target.id,
        logs_dir=options.output_dir / "logs",
        python=options.python,
        backends=options.backends,
        plugins=target.plugins,
    )
    env.root.mkdir(parents=True, exist_ok=True)

    record: dict[str, Any] = {
        "id": target.id,
        "mode": target.mode,
        "status": FAILED,
        "started_at": started_at.isoformat(),
        "netbox_version": None,
        "python_version": None,
        "plugins": [
            {
                "name": plugin.name,
                "package": plugin.package,
                "repository": plugin.repository,
                "ref": plugin.ref,
                "installed_version": None,
                "reported_version": None,
            }
            for plugin in target.plugins
        ],
        "stages": [],
    }

    checkout_log = env.log_path("checkout")
    checkout = cache.checkout(config.netbox, env.source, checkout_log)
    if not checkout.ok:
        record["stages"] = [
            {
                "name": "checkout",
                "status": FAILED,
                "returncode": checkout.returncode,
                "duration_seconds": round(checkout.duration, 1),
                "log": str(checkout_log.relative_to(options.output_dir)),
                "detail": f"cannot check out NetBox {config.netbox.ref}",
                "stderr_tail": checkout.stderr_tail,
            }
        ] + [{"name": name, "status": SKIPPED, "detail": "checkout failed"} for name in STAGE_ORDER]
        record["duration_seconds"] = round(time.monotonic() - started, 1)
        return record

    failed = False
    for name in STAGE_ORDER:
        if failed:
            record["stages"].append(
                {"name": name, "status": SKIPPED, "detail": "previous stage failed"}
            )
            continue

        log.info("[%s] stage %s", target.id, name)
        try:
            if name == "install":
                stage = stage_install(env)
            elif name == "migrate":
                stage = stage_migrate(env)
            else:
                stage = stage_startup(env, options.startup_timeout)
        except Exception as exc:  # noqa: BLE001 - провал цели не должен ронять прогон
            log.exception("[%s] stage %s crashed", target.id, name)
            stage = StageResult(name=name, status=FAILED, detail=f"runner error: {exc!r}")

        record["stages"].append(_stage_to_dict(stage, options.output_dir))
        log.info("[%s] stage %s -> %s (%.1fs)", target.id, name, stage.status, stage.duration)

        for package, version in stage.extra.get("installed_versions", {}).items():
            _set_plugin_field(record, package, "installed_version", version)
        for package, version in stage.extra.get("reported_versions", {}).items():
            _set_plugin_field(record, package, "reported_version", version)
        if stage.extra.get("netbox_version"):
            record["netbox_version"] = stage.extra["netbox_version"]
        if stage.extra.get("python_version"):
            record["python_version"] = stage.extra["python_version"]

        if not stage.ok:
            failed = True

    record["status"] = FAILED if failed else PASSED
    record["duration_seconds"] = round(time.monotonic() - started, 1)

    if not options.keep_workdir:
        _drop_workdir(env, cache, checkout_log)
    return record


def _set_plugin_field(record: dict[str, Any], package: str, field: str, value: Any) -> None:
    for entry in record["plugins"]:
        if entry["package"] == package:
            entry[field] = value


def _drop_workdir(env: TargetEnvironment, cache: SourceCache, checkout_log: Path) -> None:
    """Отпустить venv и worktree: 5 целей — это ~5 ГБ, что заметно на runner'е."""
    import shutil

    run_logged(
        ["git", "-C", str(env.source), "worktree", "remove", "--force", str(env.source)],
        checkout_log,
        env=clean_env(),
    )
    shutil.rmtree(env.root, ignore_errors=True)


def run(config: CompatConfig, options: RunOptions) -> dict[str, Any]:
    started_at = datetime.now(timezone.utc)
    started = time.monotonic()
    targets = build_targets(config, options.mode)
    cache = SourceCache(options.workdir / "_cache")

    log.info(
        "NetBox %s (%s), %d plugin(s), mode=%s -> %d target(s)",
        config.netbox.ref,
        config.netbox.repository,
        len(config.plugins),
        options.mode,
        len(targets),
    )

    records = [run_target(target, config, options, cache) for target in targets]
    passed = sum(1 for record in records if record["status"] == PASSED)
    finished_at = datetime.now(timezone.utc)

    return {
        "schema_version": 1,
        "config": str(config.path),
        "mode": options.mode,
        "netbox": {"repository": config.netbox.repository, "ref": config.netbox.ref},
        "status": PASSED if passed == len(records) and records else FAILED,
        "started_at": started_at.isoformat(),
        "finished_at": finished_at.isoformat(),
        "duration_seconds": round(time.monotonic() - started, 1),
        "totals": {
            "targets": len(records),
            "passed": passed,
            "failed": len(records) - passed,
        },
        "targets": records,
    }
