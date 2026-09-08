"""CLI раннера. Одна и та же команда используется локально и в CI."""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

from .config import ConfigError, load_config
from .environment import Backends
from .report import write_job_summary, write_json, write_markdown
from .runner import MODE_ISOLATED, MODES, RunOptions, run
from .stages import PASSED

EXIT_OK = 0
EXIT_TESTS_FAILED = 1
EXIT_BAD_CONFIG = 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m netbox_compat",
        description="Check NetBox plugin compatibility against a target NetBox ref.",
    )
    parser.add_argument("--config", default="compatibility.yaml", type=Path, help="path to the YAML matrix")
    parser.add_argument("--mode", choices=MODES, default=MODE_ISOLATED,
                        help="isolated: one instance per plugin; combined: all plugins in one instance")
    parser.add_argument("--workdir", default=Path(".work"), type=Path, help="scratch dir for checkouts and venvs")
    parser.add_argument("--output-dir", default=Path("reports"), type=Path, help="where report.json/report.md/logs go")
    parser.add_argument("--python", default=sys.executable, help="interpreter used to create each target venv")
    parser.add_argument("--startup-timeout", type=float, default=180.0,
                        help="seconds to wait for /api/status/ to answer")
    parser.add_argument("--keep-workdir", action="store_true", help="keep checkouts and venvs for debugging")
    parser.add_argument("--validate-only", action="store_true", help="validate the config and exit")
    parser.add_argument("--verbose", action="store_true")

    group = parser.add_argument_group("backends")
    group.add_argument("--db-host", default=os.environ.get("PGHOST", "localhost"))
    group.add_argument("--db-port", type=int, default=int(os.environ.get("PGPORT", "5432")))
    group.add_argument("--db-user", default=os.environ.get("PGUSER", "netbox"))
    group.add_argument("--db-password", default=os.environ.get("PGPASSWORD", "netbox"))
    group.add_argument("--db-maintenance", default=os.environ.get("PGDATABASE", "postgres"),
                       help="existing database used to CREATE/DROP the per-target ones")
    group.add_argument("--redis-host", default=os.environ.get("REDIS_HOST", "localhost"))
    group.add_argument("--redis-port", type=int, default=int(os.environ.get("REDIS_PORT", "6379")))
    group.add_argument("--redis-password", default=os.environ.get("REDIS_PASSWORD", ""))
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )

    try:
        config = load_config(args.config)
    except ConfigError as exc:
        logging.error("invalid config: %s", exc)
        return EXIT_BAD_CONFIG

    if args.validate_only:
        logging.info(
            "config OK: NetBox %s, %d plugin(s): %s",
            config.netbox.ref,
            len(config.plugins),
            ", ".join(p.package for p in config.plugins),
        )
        return EXIT_OK

    options = RunOptions(
        workdir=args.workdir.resolve(),
        output_dir=args.output_dir.resolve(),
        python=args.python,
        mode=args.mode,
        backends=Backends(
            db_host=args.db_host,
            db_port=args.db_port,
            db_user=args.db_user,
            db_password=args.db_password,
            db_maintenance=args.db_maintenance,
            redis_host=args.redis_host,
            redis_port=args.redis_port,
            redis_password=args.redis_password,
        ),
        startup_timeout=args.startup_timeout,
        keep_workdir=args.keep_workdir,
    )
    options.workdir.mkdir(parents=True, exist_ok=True)
    options.output_dir.mkdir(parents=True, exist_ok=True)

    report = run(config, options)

    json_path = write_json(report, options.output_dir / "report.json")
    md_path = write_markdown(report, options.output_dir / "report.md")
    write_job_summary(report)

    totals = report["totals"]
    logging.info("report: %s", json_path)
    logging.info("summary: %s", md_path)
    logging.info(
        "result: %s (%d/%d targets passed)", report["status"], totals["passed"], totals["targets"]
    )
    return EXIT_OK if report["status"] == PASSED else EXIT_TESTS_FAILED
