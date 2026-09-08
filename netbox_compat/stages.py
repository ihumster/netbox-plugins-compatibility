"""Стадии проверки: install -> migrate -> startup.

Каждая стадия пишет полный вывод в собственный лог и возвращает StageResult.
Успех стадии подтверждается содержательно, а не только нулевым кодом возврата:
install сверяет, что все пакеты из конфига действительно установлены, migrate —
что неприменённых миграций не осталось, startup — что живой инстанс отдаёт
плагины в /api/status/.
"""

from __future__ import annotations

import json
import socket
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .environment import TargetEnvironment
from .process import CommandResult, clean_env, run_logged, tail

STAGE_ORDER = ("install", "migrate", "startup")

PASSED = "passed"
FAILED = "failed"
SKIPPED = "skipped"

# Резолвит установленную версию каждого пакета, не импортируя его: импорт
# плагина вне Django-контекста падает у половины из них.
VERSION_PROBE = """
import json, sys
from importlib.metadata import PackageNotFoundError, packages_distributions, version

mapping = packages_distributions()
result = {}
for package in sys.argv[1:]:
    dists = mapping.get(package)
    if not dists:
        result[package] = None
        continue
    try:
        result[package] = version(dists[0])
    except PackageNotFoundError:
        result[package] = None
print(json.dumps(result))
"""

# Каждой цели — своя пустая БД и чистый Redis, иначе миграции предыдущей цели
# засчитываются следующей.
RESET_BACKENDS = """
import json, sys

params = json.loads(sys.argv[1])

import psycopg

conn = psycopg.connect(
    host=params["db_host"], port=params["db_port"], user=params["db_user"],
    password=params["db_password"], dbname=params["db_maintenance"], autocommit=True,
)
with conn.cursor() as cur:
    cur.execute('DROP DATABASE IF EXISTS "%s" WITH (FORCE)' % params["db_name"])
    cur.execute('CREATE DATABASE "%s"' % params["db_name"])
conn.close()

import redis

for index in (0, 1):
    redis.Redis(
        host=params["redis_host"], port=params["redis_port"],
        password=params["redis_password"] or None, db=index,
    ).flushdb()

print("prepared database %s and redis db 0,1" % params["db_name"])
"""


@dataclass
class StageResult:
    name: str
    status: str
    returncode: int | None = None
    duration: float = 0.0
    log: str | None = None
    detail: str = ""
    stdout_tail: list[str] = field(default_factory=list)
    stderr_tail: list[str] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status == PASSED


def _from_command(name: str, result: CommandResult, detail: str) -> StageResult:
    return StageResult(
        name=name,
        status=FAILED,
        returncode=result.returncode,
        log=str(result.log_path),
        detail=detail,
        stdout_tail=result.stdout_tail,
        stderr_tail=result.stderr_tail,
    )


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def stage_install(env: TargetEnvironment) -> StageResult:
    """venv с целевой версией NetBox и всеми плагинами цели."""
    log = env.log_path("install")
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text("", encoding="utf-8")
    started = time.monotonic()
    child_env = clean_env()

    steps: list[list[str]] = [
        [env.python, "-m", "venv", str(env.venv)],
        [str(env.venv_python), "-m", "pip", "install", "--quiet", "--upgrade", "pip", "wheel"],
        [
            str(env.venv_python), "-m", "pip", "install", "--quiet",
            "-r", str(env.source / "requirements.txt"),
        ],
    ]
    steps += [
        [str(env.venv_python), "-m", "pip", "install", "--quiet", plugin.pip_spec]
        for plugin in env.plugins
    ]

    for step in steps:
        result = run_logged(step, log, cwd=env.source, env=child_env)
        if not result.ok:
            stage = _from_command("install", result, f"command failed: {' '.join(step[-2:])}")
            stage.duration = time.monotonic() - started
            return stage

    probe = run_logged(
        [str(env.venv_python), "-c", VERSION_PROBE, *[p.package for p in env.plugins]],
        log,
        cwd=env.source,
        env=child_env,
    )
    duration = time.monotonic() - started
    if not probe.ok:
        stage = _from_command("install", probe, "could not resolve installed plugin versions")
        stage.duration = duration
        return stage

    versions: dict[str, str | None] = json.loads(probe.stdout)
    missing = sorted(pkg for pkg, ver in versions.items() if ver is None)
    if missing:
        return StageResult(
            name="install",
            status=FAILED,
            returncode=probe.returncode,
            duration=duration,
            log=str(log),
            detail=f"pip reported success but these packages are not importable: {', '.join(missing)}",
            stdout_tail=probe.stdout_tail,
        )

    return StageResult(
        name="install",
        status=PASSED,
        returncode=0,
        duration=duration,
        log=str(log),
        extra={"installed_versions": versions},
    )


def stage_migrate(env: TargetEnvironment) -> StageResult:
    """Чистая БД, сгенерированный configuration.py и применённые миграции."""
    log = env.log_path("migrate")
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text("", encoding="utf-8")
    started = time.monotonic()
    child_env = clean_env()
    backends = env.backends

    reset = run_logged(
        [
            str(env.venv_python), "-c", RESET_BACKENDS,
            json.dumps(
                {
                    "db_host": backends.db_host,
                    "db_port": backends.db_port,
                    "db_user": backends.db_user,
                    "db_password": backends.db_password,
                    "db_maintenance": backends.db_maintenance,
                    "db_name": env.database_name,
                    "redis_host": backends.redis_host,
                    "redis_port": backends.redis_port,
                    "redis_password": backends.redis_password,
                }
            ),
        ],
        log,
        env=child_env,
    )
    if not reset.ok:
        stage = _from_command("migrate", reset, "could not prepare Postgres/Redis for this target")
        stage.duration = time.monotonic() - started
        return stage

    env.render_configuration()
    manage_dir = env.source / "netbox"

    migrate = run_logged(
        [str(env.venv_python), "manage.py", "migrate", "--no-input"],
        log,
        cwd=manage_dir,
        env=child_env,
    )
    if not migrate.ok:
        stage = _from_command("migrate", migrate, "manage.py migrate failed")
        stage.duration = time.monotonic() - started
        return stage

    # Содержательная проверка: миграции плагинов действительно применены.
    check = run_logged(
        [str(env.venv_python), "manage.py", "migrate", "--check"],
        log,
        cwd=manage_dir,
        env=child_env,
    )
    duration = time.monotonic() - started
    if not check.ok:
        stage = _from_command("migrate", check, "unapplied migrations remain after migrate")
        stage.duration = duration
        return stage

    return StageResult(name="migrate", status=PASSED, returncode=0, duration=duration, log=str(log))


def _fetch_status(port: int, timeout: float) -> dict[str, Any]:
    # ProxyHandler({}) обязателен: в окружении задан HTTPS_PROXY, и без этого
    # запрос к 127.0.0.1 уходит в прокси.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(f"http://127.0.0.1:{port}/api/status/", timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def _wait_for_status(port: int, proc: subprocess.Popen, deadline: float) -> tuple[dict | None, str]:
    last_error = "no response before timeout"
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            return None, f"server exited with code {proc.returncode} before becoming ready"
        try:
            return _fetch_status(port, timeout=5), ""
        except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
            last_error = f"{type(exc).__name__}: {exc}"
        time.sleep(1.0)
    return None, last_error


def stage_startup(env: TargetEnvironment, timeout: float) -> StageResult:
    """Живой инстанс отвечает на /api/status/ и показывает все плагины."""
    log = env.log_path("startup")
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text("", encoding="utf-8")
    started = time.monotonic()
    port = _free_port()
    expected = [plugin.package for plugin in env.plugins]

    with log.open("a", encoding="utf-8") as fh:
        fh.write(f"$ manage.py runserver 127.0.0.1:{port} --noreload\n--- output ---\n")
        fh.flush()
        proc = subprocess.Popen(
            [str(env.venv_python), "manage.py", "runserver", f"127.0.0.1:{port}", "--noreload"],
            cwd=env.source / "netbox",
            env=clean_env(),
            stdout=fh,
            stderr=subprocess.STDOUT,
        )
        try:
            payload, error = _wait_for_status(port, proc, time.monotonic() + timeout)
        finally:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=10)

    duration = time.monotonic() - started
    output_tail = tail(log.read_text(encoding="utf-8", errors="replace"))

    if payload is None:
        return StageResult(
            name="startup",
            status=FAILED,
            returncode=proc.returncode,
            duration=duration,
            log=str(log),
            detail=f"/api/status/ never became available: {error}",
            stderr_tail=output_tail,
        )

    reported = payload.get("plugins") or {}
    missing = [package for package in expected if package not in reported]
    extra = {
        "netbox_version": payload.get("netbox-full-version") or payload.get("netbox-version"),
        "python_version": payload.get("python-version"),
        "reported_versions": {pkg: reported.get(pkg) for pkg in expected},
        "status_url": f"http://127.0.0.1:{port}/api/status/",
    }
    if missing:
        return StageResult(
            name="startup",
            status=FAILED,
            returncode=0,
            duration=duration,
            log=str(log),
            detail=f"instance is up but /api/status/ does not list: {', '.join(missing)}",
            stderr_tail=output_tail,
            extra=extra,
        )

    return StageResult(
        name="startup",
        status=PASSED,
        returncode=0,
        duration=duration,
        log=str(log),
        extra=extra,
    )
