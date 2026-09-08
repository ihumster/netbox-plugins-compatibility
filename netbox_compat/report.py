"""Артефакты прогона: report.json, report.md и GitHub job summary."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from .stages import FAILED, PASSED, SKIPPED, STAGE_ORDER

ICONS = {PASSED: "✅", FAILED: "❌", SKIPPED: "⏭️", "checkout": "❌"}


def _icon(status: str) -> str:
    return ICONS.get(status, "❔")


def _duration(seconds: float) -> str:
    minutes, secs = divmod(int(seconds), 60)
    return f"{minutes}m {secs}s" if minutes else f"{secs}s"


def _stage_map(target: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {stage["name"]: stage for stage in target["stages"]}


def write_json(report: dict[str, Any], path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


def render_markdown(report: dict[str, Any]) -> str:
    totals = report["totals"]
    netbox = report["netbox"]
    runtime = next(
        (t["netbox_version"] for t in report["targets"] if t.get("netbox_version")), None
    )
    python = next(
        (t["python_version"] for t in report["targets"] if t.get("python_version")), None
    )

    lines = [
        "# NetBox plugin compatibility",
        "",
        f"**Result:** {_icon(report['status'])} `{report['status']}` — "
        f"{totals['passed']}/{totals['targets']} targets passed",
        "",
        f"- **Target NetBox:** `{netbox['ref']}` ({netbox['repository']})",
        f"- **NetBox at runtime:** `{runtime or 'n/a'}` · **Python at runtime:** `{python or 'n/a'}`",
        f"- **Mode:** `{report['mode']}` · **Started:** {report['started_at']} · "
        f"**Duration:** {_duration(report['duration_seconds'])}",
        "",
        "## Targets",
        "",
        "| Target | Mode | " + " | ".join(s.capitalize() for s in STAGE_ORDER) + " | Duration |",
        "| --- | --- | " + " | ".join("---" for _ in STAGE_ORDER) + " | --- |",
    ]

    for target in report["targets"]:
        stages = _stage_map(target)
        cells = []
        for name in STAGE_ORDER:
            stage = stages.get(name)
            cells.append(f"{_icon(stage['status'])} {stage['status']}" if stage else "—")
        lines.append(
            f"| `{target['id']}` | {target['mode']} | " + " | ".join(cells) +
            f" | {_duration(target.get('duration_seconds', 0))} |"
        )

    lines += [
        "",
        "## Plugins",
        "",
        "| Plugin | Package | Ref | Installed | Target | " +
        " | ".join(s.capitalize() for s in STAGE_ORDER) + " |",
        "| --- | --- | --- | --- | --- | " + " | ".join("---" for _ in STAGE_ORDER) + " |",
    ]
    for target in report["targets"]:
        stages = _stage_map(target)
        cells = [_icon(stages[name]["status"]) if name in stages else "—" for name in STAGE_ORDER]
        for plugin in target["plugins"]:
            lines.append(
                f"| {plugin['name']} | `{plugin['package']}` | `{plugin['ref']}` | "
                f"`{plugin['installed_version'] or 'n/a'}` | `{target['id']}` | "
                + " | ".join(cells) + " |"
            )

    if report.get("resolution"):
        lines += _render_resolution(report["resolution"])

    failures = [
        (target, stage)
        for target in report["targets"]
        for stage in target["stages"]
        if stage["status"] == FAILED
    ]
    if failures:
        lines += ["", "## Failures", ""]
        for target, stage in failures:
            lines.append(
                f"### `{target['id']}` / {stage['name']} "
                f"(exit {stage.get('returncode')})"
            )
            if stage.get("detail"):
                lines.append(f"{stage['detail']}")
            output = stage.get("stderr_tail") or stage.get("stdout_tail") or []
            if output:
                lines += ["", "```", *output, "```"]
            if stage.get("log"):
                lines.append(f"Full log: `{stage['log']}`")
            lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def write_markdown(report: dict[str, Any], path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_markdown(report), encoding="utf-8")
    return path


def write_job_summary(report: dict[str, Any]) -> Path | None:
    """Продублировать сводку в GitHub Actions job summary, если он доступен."""
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if not summary:
        return None
    path = Path(summary)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(render_markdown(report))
    return path


def _render_resolution(resolution: dict[str, Any]) -> list[str]:
    """Таблица найденных ref'ов для режима --resolve."""
    totals = resolution["totals"]
    lines = [
        "",
        "## Resolution",
        "",
        f"Против NetBox `{resolution.get('netbox_version') or 'unknown'}`: "
        f"{totals['resolved']}/{totals['plugins']} плагинов разрешены "
        f"(`--max-attempts {resolution['max_attempts']}`).",
        "",
        "| Plugin | Configured | Resolved | Rejected by declared bounds | Attempted | Note |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for entry in resolution["plugins"]:
        resolved = f"`{entry['resolved_ref']}`" if entry["resolved_ref"] else "—"
        icon = "✅" if entry["status"] == "resolved" else "❌"
        note = entry.get("detail", "")
        if not note and entry["resolved_ref"] == entry["configured_ref"]:
            note = "уже актуален"
        lines.append(
            f"| {entry['name']} | `{entry['configured_ref']}` | {icon} {resolved} | "
            f"{entry['tags_rejected']} | {len(entry['attempts'])} | {note} |"
        )

    for entry in resolution["plugins"]:
        if not entry["attempts"]:
            continue
        lines += ["", f"<details><summary><code>{entry['package']}</code> — попытки</summary>", ""]
        for attempt in entry["attempts"]:
            icon = "✅" if attempt["status"] == "passed" else "❌"
            reason = f" — {attempt['failed_stage']}: {attempt['detail']}" if attempt["failed_stage"] else ""
            lines.append(f"- {icon} `{attempt['ref']}`{reason}")
        lines += ["", "</details>"]
    return lines
