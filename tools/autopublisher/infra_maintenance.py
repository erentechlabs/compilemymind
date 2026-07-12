#!/usr/bin/env python3
"""Prepare and validate low-risk infrastructure maintenance updates.

This script is intentionally conservative. It may update local version files and
lockfiles, but the workflow commits only after the generated site builds.
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


def version_risk(current: str, latest: str) -> str:
    current_version = parse_version(current)
    latest_version = parse_version(latest)
    if latest_version <= current_version:
        return "none"
    if latest_version[0] != current_version[0]:
        return "high"
    if latest_version[1] != current_version[1]:
        return "high"
    return "low"


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


def npm_executable() -> str | None:
    for candidate in ("npm.cmd", "npm.exe", "npm"):
        path = shutil.which(candidate)
        if path and Path(path).suffix.lower() not in {".ps1", ".psm1"}:
            return path
    return None


def package_summary(theme_dir: Path) -> dict[str, Any]:
    npm = npm_executable()
    if not npm or not (theme_dir / "package.json").exists():
        return {"available": False, "reason": "npm or package.json unavailable"}
    outdated = run([npm, "outdated", "--json"], cwd=theme_dir, timeout=120)
    outdated_json: dict[str, Any] = {}
    if outdated["stdout"]:
        try:
            outdated_json = json.loads(outdated["stdout"])
        except json.JSONDecodeError:
            outdated_json = {"raw": outdated["stdout"]}
    updates = []
    for name, info in outdated_json.items():
        if not isinstance(info, dict):
            continue
        current = str(info.get("current") or "")
        latest = str(info.get("latest") or "")
        updates.append(
            {
                "name": name,
                "current": current,
                "wanted": str(info.get("wanted") or ""),
                "latest": latest,
                "risk": version_risk(current, latest),
            }
        )
    return {
        "available": True,
        "outdated": outdated,
        "outdated_json": outdated_json,
        "updates": updates,
        "lockfile": (theme_dir / "package-lock.json").exists(),
    }


def apply_npm_updates(theme_dir: Path) -> dict[str, Any]:
    npm = npm_executable()
    if not npm or not (theme_dir / "package.json").exists():
        return {"skipped": "npm or package.json unavailable"}
    if not (theme_dir / "package-lock.json").exists():
        return {"skipped": "package-lock.json is absent; no dependency lockfile was mutated"}
    return run([npm, "update", "--package-lock-only", "--ignore-scripts"], cwd=theme_dir, timeout=240)


def write_report(data: dict[str, Any]) -> Path:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    now = dt.datetime.now(dt.timezone.utc)
    path = REPORT_DIR / f"infrastructure-maintenance-{now.date().isoformat()}.md"
    lines = [
        "# Infrastructure Maintenance Report",
        "",
        f"Generated: {now.replace(microsecond=0).isoformat().replace('+00:00', 'Z')}",
        "",
        "This report was created by the autonomous maintenance workflow. Safe changes are committed automatically only after validation passes.",
        "",
        "## Decision",
        "",
        f"- Manual review required: `{data.get('manual_review_required', False)}`",
        f"- Safe changes: `{', '.join(data.get('safe_changes', [])) or 'none'}`",
        f"- Review reasons: {', '.join(data.get('manual_review_reasons', [])) or 'none'}",
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

    data: dict[str, Any] = {
        "validation": [],
        "safe_changes": [],
        "manual_review_reasons": [],
        "manual_review_required": False,
    }
    version_path = ROOT / ".hugo-version"
    original_hugo_version = version_path.read_bytes() if version_path.exists() else None
    package_lock_path = ROOT / "themes" / "mana" / "package-lock.json"
    original_package_lock = package_lock_path.read_bytes() if package_lock_path.exists() else None
    current = version_path.read_text(encoding="utf-8").strip() if version_path.exists() else ""
    data["current_hugo"] = current
    try:
        latest = latest_hugo_version()
    except Exception as error:
        latest = None
        data["hugo_error"] = str(error)
    data["latest_hugo"] = latest
    data["hugo_action"] = "none"
    hugo_risk = version_risk(current, latest or current)
    data["hugo_risk"] = hugo_risk

    hugo = shutil.which("hugo")
    baseline = run([hugo, "--minify"], timeout=240) if hugo else {
        "command": "hugo --minify",
        "returncode": 127,
        "stderr": "Hugo is not installed",
    }
    data["validation"].append({"name": "baseline", **baseline})
    if baseline["returncode"] != 0:
        data["manual_review_required"] = True
        data["manual_review_reasons"].append("baseline Hugo build failed")

    if latest and parse_version(latest) > parse_version(current):
        if hugo_risk == "low" and args.apply_safe_updates and baseline["returncode"] == 0:
            version_path.write_text(latest + "\n", encoding="utf-8")
            data["hugo_action"] = f"updated .hugo-version to low-risk patch {latest}"
            data["safe_changes"].append(f"Hugo patch {current} -> {latest}")
        else:
            data["hugo_action"] = f"candidate update available: {latest}; manual review required"
            data["manual_review_required"] = True
            data["manual_review_reasons"].append(f"Hugo {hugo_risk}-risk update {current} -> {latest}")

    theme_dir = ROOT / "themes" / "mana"
    data["npm"] = package_summary(theme_dir)
    npm_updates = data["npm"].get("updates", [])
    npm_high_risk = [item for item in npm_updates if item.get("risk") == "high"]
    if npm_high_risk:
        data["manual_review_required"] = True
        data["manual_review_reasons"].append(
            "npm major/minor updates: " + ", ".join(item["name"] for item in npm_high_risk)
        )
        if data["safe_changes"]:
            if original_hugo_version is None:
                version_path.unlink(missing_ok=True)
            else:
                version_path.write_bytes(original_hugo_version)
            data["safe_changes"] = []
    if args.apply_safe_updates and baseline["returncode"] == 0 and not data["manual_review_required"]:
        safe_npm = [item for item in npm_updates if item.get("risk") == "low"]
        if safe_npm and data["npm"].get("lockfile"):
            data["npm_update"] = apply_npm_updates(theme_dir)
            if data["npm_update"].get("returncode") == 0:
                data["safe_changes"].append("npm patch updates in themes/mana/package-lock.json")
        elif safe_npm:
            data["npm_update"] = {"skipped": "safe updates found but no package-lock.json exists"}

    post_update = run([hugo, "--minify"], timeout=240) if hugo and data["safe_changes"] else None
    if post_update is not None:
        data["validation"].append({"name": "after_safe_updates", **post_update})
        if post_update["returncode"] != 0:
            data["manual_review_required"] = True
            data["manual_review_reasons"].append("Hugo build failed after safe updates")
            if original_hugo_version is None:
                version_path.unlink(missing_ok=True)
            else:
                version_path.write_bytes(original_hugo_version)
            if original_package_lock is None:
                package_lock_path.unlink(missing_ok=True)
            else:
                package_lock_path.write_bytes(original_package_lock)
            data["safe_changes"] = []
            data["rollback"] = "safe update candidates were reverted after the regression test failed"

    report_path = write_report(data)
    print(json.dumps({"report": str(report_path.relative_to(ROOT)), **data}, ensure_ascii=False, indent=2))
    return 0 if all(check.get("returncode") == 0 for check in data["validation"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
