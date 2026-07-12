#!/usr/bin/env python3
"""Prepare low-risk infrastructure maintenance updates for manual review.

This script is intentionally conservative. It may update local version files and
lockfiles inside a GitHub Actions branch, but the workflow that runs it opens a
pull request instead of pushing directly to the production branch.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import urllib.request
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
REPORT_DIR = ROOT / ".autopublisher" / "reports"


def run(command: list[str], cwd: Path = ROOT, timeout: int = 180) -> dict[str, Any]:
    result = subprocess.run(command, cwd=cwd, text=True, capture_output=True, timeout=timeout)
    return {
        "command": " ".join(command),
        "returncode": result.returncode,
        "stdout": result.stdout.strip(),
        "stderr": result.stderr.strip(),
    }


def parse_version(value: str) -> tuple[int, int, int]:
    match = re.search(r"(\d+)\.(\d+)\.(\d+)", value)
    if not match:
        return (0, 0, 0)
    return tuple(int(part) for part in match.groups())  # type: ignore[return-value]


def latest_hugo_version() -> str | None:
    request = urllib.request.Request(
        "https://api.github.com/repos/gohugoio/hugo/releases/latest",
        headers={"User-Agent": "CompileMyMindInfraMaintenance/1.0"},
    )
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(request, timeout=30) as response:
        payload = json.loads(response.read().decode("utf-8"))
    tag = str(payload.get("tag_name", "")).lstrip("v")
    return tag or None


def package_summary(theme_dir: Path) -> dict[str, Any]:
    npm = shutil.which("npm") or shutil.which("npm.cmd")
    if not npm or not (theme_dir / "package.json").exists():
        return {"available": False, "reason": "npm or package.json unavailable"}
    install = run([npm, "install", "--package-lock-only"], cwd=theme_dir, timeout=240)
    outdated = run([npm, "outdated", "--json"], cwd=theme_dir, timeout=120)
    outdated_json: dict[str, Any] = {}
    if outdated["stdout"]:
        try:
            outdated_json = json.loads(outdated["stdout"])
        except json.JSONDecodeError:
            outdated_json = {"raw": outdated["stdout"]}
    return {"available": True, "install": install, "outdated": outdated, "outdated_json": outdated_json}


def apply_npm_updates(theme_dir: Path) -> dict[str, Any]:
    npm = shutil.which("npm") or shutil.which("npm.cmd")
    if not npm or not (theme_dir / "package.json").exists():
        return {"skipped": "npm or package.json unavailable"}
    return run([npm, "update", "--package-lock-only"], cwd=theme_dir, timeout=240)


def write_report(data: dict[str, Any]) -> Path:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    path = REPORT_DIR / f"infrastructure-maintenance-{dt.datetime.utcnow().date().isoformat()}.md"
    lines = [
        "# Infrastructure Maintenance Report",
        "",
        f"Generated: {dt.datetime.utcnow().replace(microsecond=0).isoformat()}Z",
        "",
        "This report was created by the autonomous maintenance workflow. Changes are prepared for pull request review and are not deployed automatically.",
        "",
        "## Hugo",
        "",
        f"- Current: `{data.get('current_hugo', 'unknown')}`",
        f"- Latest: `{data.get('latest_hugo', 'unknown')}`",
        f"- Action: {data.get('hugo_action', 'none')}",
        "",
        "## npm Theme Tooling",
        "",
    ]
    npm_summary = data.get("npm", {})
    if npm_summary.get("available"):
        outdated = npm_summary.get("outdated_json") or {}
        if outdated:
            lines.append("Outdated packages detected:")
            lines.append("")
            for name, info in outdated.items():
                if isinstance(info, dict):
                    lines.append(f"- `{name}` current `{info.get('current')}` wanted `{info.get('wanted')}` latest `{info.get('latest')}`")
                else:
                    lines.append(f"- `{name}`: `{info}`")
        else:
            lines.append("No npm package updates were reported.")
    else:
        lines.append(f"npm check skipped: {npm_summary.get('reason', 'unknown')}")
    lines.extend(
        [
            "",
            "## Validation",
            "",
        ]
    )
    for check in data.get("validation", []):
        status = "passed" if check.get("returncode") == 0 else "failed"
        lines.append(f"- `{check.get('command')}`: {status}")
        if check.get("stderr"):
            lines.append("")
            lines.append("```text")
            lines.append(str(check["stderr"])[-2000:])
            lines.append("```")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description="Prepare safe infrastructure updates.")
    parser.add_argument("--apply-safe-updates", action="store_true", help="Apply candidate updates in the current branch.")
    args = parser.parse_args()

    data: dict[str, Any] = {"validation": []}
    version_path = ROOT / ".hugo-version"
    current = version_path.read_text(encoding="utf-8").strip() if version_path.exists() else ""
    data["current_hugo"] = current
    try:
        latest = latest_hugo_version()
    except Exception as error:
        latest = None
        data["hugo_error"] = str(error)
    data["latest_hugo"] = latest
    data["hugo_action"] = "none"

    if latest and parse_version(latest) > parse_version(current):
        if args.apply_safe_updates:
            version_path.write_text(latest + "\n", encoding="utf-8")
            data["hugo_action"] = f"updated .hugo-version to {latest} for PR validation"
        else:
            data["hugo_action"] = f"candidate update available: {latest}"

    theme_dir = ROOT / "themes" / "mana"
    data["npm"] = package_summary(theme_dir)
    if args.apply_safe_updates:
        data["npm_update"] = apply_npm_updates(theme_dir)

    hugo = shutil.which("hugo")
    if hugo:
        data["validation"].append(run([hugo, "--minify"], timeout=240))
    else:
        data["validation"].append({"command": "hugo --minify", "returncode": 127, "stderr": "Hugo is not installed"})

    report_path = write_report(data)
    print(json.dumps({"report": str(report_path.relative_to(ROOT)), **data}, ensure_ascii=False, indent=2))
    return 0 if all(check.get("returncode") == 0 for check in data["validation"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
