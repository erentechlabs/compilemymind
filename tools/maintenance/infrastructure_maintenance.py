#!/usr/bin/env python3
"""Prepare Hugo, theme, and Cloudflare-compatible infrastructure updates."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
REPORT_DIR = ROOT / ".maintenance" / "reports"
THEME_DIR = ROOT / "themes" / "mana"


def run(command: list[str], cwd: Path = ROOT, timeout: int = 240) -> dict[str, Any]:
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
        return "medium"
    return "low"


def release_note_risk_terms(notes: str) -> list[str]:
    terms = ("breaking", "removed", "deprecated", "deprecation", "migration", "incompatible")
    lowered = notes.lower()
    return [term for term in terms if re.search(rf"\b{re.escape(term)}\b", lowered)]


def update_readme_hugo_version(version: str, readme_path: Path | None = None) -> None:
    """Keep the README badge and Tech Stack version aligned with the Hugo pin."""
    path = readme_path or (ROOT / "README.md")
    text = path.read_text(encoding="utf-8")
    updated, badge_count = re.subn(r"Hugo-v\d+\.\d+\.\d+-", f"Hugo-v{version}-", text)
    updated, framework_count = re.subn(
        r"(\*\*Framework:\*\* \[Hugo\]\(https://gohugo\.io/\) v)\d+\.\d+\.\d+",
        rf"\g<1>{version}",
        updated,
    )
    if badge_count != 1 or framework_count != 1:
        raise RuntimeError("README must contain exactly one Hugo badge and one Tech Stack version")
    path.write_text(updated, encoding="utf-8")


def npm_executable() -> str | None:
    for candidate in ("npm.cmd", "npm.exe", "npm"):
        path = shutil.which(candidate)
        if path and Path(path).suffix.lower() not in {".ps1", ".psm1"}:
            return path
    return None


def declared_theme_dependencies(theme_dir: Path = THEME_DIR) -> dict[str, str]:
    package_path = theme_dir / "package.json"
    if not package_path.exists():
        return {}
    package = json.loads(package_path.read_text(encoding="utf-8"))
    dependencies: dict[str, str] = {}
    for section in ("dependencies", "devDependencies"):
        values = package.get(section, {})
        if isinstance(values, dict):
            dependencies.update({str(name): str(version) for name, version in values.items()})
    return dependencies


def dependency_baseline(specifier: str) -> str:
    match = re.search(r"\d+\.\d+\.\d+", specifier)
    return match.group(0) if match else "0.0.0"


def infrastructure_inventory(root: Path = ROOT) -> dict[str, Any]:
    workflows: dict[str, list[str]] = {}
    for path in sorted((root / ".github" / "workflows").glob("*.yml")):
        actions = sorted(set(re.findall(r"\buses:\s*([^\s#]+)", path.read_text(encoding="utf-8"))))
        workflows[path.name] = actions
    theme_dir = root / "themes" / "mana"
    package_path = theme_dir / "package.json"
    package = json.loads(package_path.read_text(encoding="utf-8")) if package_path.exists() else {}
    return {
        "python": sys.version.split()[0],
        "hugo_pin": (root / ".hugo-version").read_text(encoding="utf-8").strip()
        if (root / ".hugo-version").exists()
        else "",
        "workflow_actions": workflows,
        "theme": {
            "name": package.get("name", ""),
            "version": package.get("version", ""),
            "dependencies": {
                **(package.get("dependencies", {}) or {}),
                **(package.get("devDependencies", {}) or {}),
            },
            "lockfile": (theme_dir / "package-lock.json").exists(),
        },
    }


def latest_hugo_release() -> dict[str, str] | None:
    request = urllib.request.Request(
        "https://api.github.com/repos/gohugoio/hugo/releases/latest",
        headers={"User-Agent": "CompileMyMindInfrastructureMaintenance/2.0"},
    )
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(request, timeout=30) as response:
        payload = json.loads(response.read().decode("utf-8"))
    version = str(payload.get("tag_name", "")).lstrip("v")
    if not version:
        return None
    return {
        "version": version,
        "url": str(payload.get("html_url", "")),
        "published_at": str(payload.get("published_at", "")),
        "notes": str(payload.get("body", ""))[:8000],
    }


def theme_dependency_summary(theme_dir: Path = THEME_DIR) -> dict[str, Any]:
    npm = npm_executable()
    dependencies = declared_theme_dependencies(theme_dir)
    if not npm or not dependencies:
        return {
            "available": False,
            "reason": "npm or theme package dependencies are unavailable",
            "updates": [],
        }
    result = run([npm, "outdated", "--json"], cwd=theme_dir, timeout=120)
    payload: dict[str, Any] = {}
    if result["stdout"]:
        try:
            parsed = json.loads(result["stdout"])
            if isinstance(parsed, dict):
                payload = parsed
        except json.JSONDecodeError:
            return {
                "available": False,
                "reason": "npm outdated returned invalid JSON",
                "command": result,
                "updates": [],
            }
    updates: list[dict[str, str]] = []
    for name, info in payload.items():
        if not isinstance(info, dict) or name not in dependencies:
            continue
        current = str(info.get("current") or dependency_baseline(dependencies[name]))
        latest = str(info.get("latest") or current)
        updates.append(
            {
                "name": name,
                "declared": dependencies[name],
                "current": current,
                "wanted": str(info.get("wanted") or current),
                "latest": latest,
                "risk": version_risk(current, latest),
            }
        )
    return {
        "available": True,
        "command": result,
        "updates": updates,
        "lockfile": (theme_dir / "package-lock.json").exists(),
    }


def apply_theme_candidates(updates: list[dict[str, str]], theme_dir: Path = THEME_DIR) -> dict[str, Any]:
    npm = npm_executable()
    if not npm:
        return {"returncode": 127, "stderr": "npm is unavailable"}
    packages = [f"{item['name']}@{item['latest']}" for item in updates]
    if not packages:
        return {"returncode": 0, "stdout": "No theme dependency updates were needed.", "stderr": ""}
    return run(
        [npm, "install", "--save-dev", "--package-lock-only", "--ignore-scripts", *packages],
        cwd=theme_dir,
        timeout=300,
    )


def write_report(data: dict[str, Any]) -> Path:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    now = dt.datetime.now(dt.timezone.utc)
    path = REPORT_DIR / f"infrastructure-maintenance-{now.date().isoformat()}.md"
    lines = [
        "# Infrastructure Maintenance Report",
        "",
        f"Generated: {now.replace(microsecond=0).isoformat().replace('+00:00', 'Z')}",
        "",
        "This report covers Hugo, the Mana theme dependencies, and Cloudflare Pages compatibility. It does not inspect, generate, revise, or publish blog content.",
        "",
        "## Decision",
        "",
        f"- Manual review required: `{data.get('manual_review_required', False)}`",
        f"- Safe changes: `{', '.join(data.get('safe_changes', [])) or 'none'}`",
        f"- Review candidates: `{', '.join(data.get('review_changes', [])) or 'none'}`",
        f"- Review reasons: {', '.join(data.get('manual_review_reasons', [])) or 'none'}",
        "",
        "## Hugo",
        "",
        f"- Current: `{data.get('current_hugo', 'unknown')}`",
        f"- Latest: `{data.get('latest_hugo', 'unknown')}`",
        f"- Risk: `{data.get('hugo_risk', 'unknown')}`",
        f"- Release: {data.get('hugo_release_url', 'not available')}",
        f"- Release-note risk terms: `{', '.join(data.get('hugo_release_note_risks', [])) or 'none'}`",
        "",
        "## Mana theme dependencies",
        "",
    ]
    updates = data.get("theme", {}).get("updates", [])
    if updates:
        for item in updates:
            lines.append(
                f"- `{item['name']}`: `{item['current']}` -> `{item['latest']}` ({item['risk']} risk)"
            )
    else:
        lines.append("No theme dependency updates were reported.")
    lines.extend(
        [
            "",
            "## Infrastructure inventory",
            "",
            "```json",
            json.dumps(data.get("inventory", {}), ensure_ascii=False, indent=2),
            "```",
            "",
            "## Validation",
            "",
        ]
    )
    for check in data.get("validation", []):
        status = "passed" if check.get("returncode") == 0 else "failed"
        lines.append(f"- `{check.get('command')}`: {status}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def restore(path: Path, original: bytes | None) -> None:
    if original is None:
        path.unlink(missing_ok=True)
    else:
        path.write_bytes(original)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply-updates", action="store_true", help="Write candidates for validation.")
    args = parser.parse_args()

    version_path = ROOT / ".hugo-version"
    readme_path = ROOT / "README.md"
    package_path = THEME_DIR / "package.json"
    lock_path = THEME_DIR / "package-lock.json"
    originals = {
        version_path: version_path.read_bytes() if version_path.exists() else None,
        readme_path: readme_path.read_bytes() if readme_path.exists() else None,
        package_path: package_path.read_bytes() if package_path.exists() else None,
        lock_path: lock_path.read_bytes() if lock_path.exists() else None,
    }
    data: dict[str, Any] = {
        "validation": [],
        "safe_changes": [],
        "review_changes": [],
        "manual_review_reasons": [],
        "manual_review_required": False,
        "inventory": infrastructure_inventory(),
    }
    current = version_path.read_text(encoding="utf-8").strip() if version_path.exists() else ""
    data["current_hugo"] = current
    hugo = shutil.which("hugo")
    baseline = run([hugo, "--minify"]) if hugo else {
        "command": "hugo --minify",
        "returncode": 127,
        "stdout": "",
        "stderr": "Hugo is unavailable",
    }
    data["validation"].append(baseline)
    if baseline["returncode"] != 0:
        data["manual_review_required"] = True
        data["manual_review_reasons"].append("baseline Hugo build failed")

    try:
        release = latest_hugo_release()
    except Exception as error:
        release = None
        data["hugo_error"] = str(error)
    latest = release.get("version", "") if release else ""
    notes = release.get("notes", "") if release else ""
    hugo_risk = version_risk(current, latest or current)
    note_risks = release_note_risk_terms(notes)
    data.update(
        {
            "latest_hugo": latest,
            "hugo_risk": hugo_risk,
            "hugo_release_url": release.get("url", "") if release else "",
            "hugo_release_note_risks": note_risks,
        }
    )
    if latest and parse_version(latest) > parse_version(current):
        safe_hugo = hugo_risk == "low" and not note_risks
        if not safe_hugo:
            data["manual_review_required"] = True
            data["manual_review_reasons"].append(
                f"Hugo {hugo_risk}-risk update {current} -> {latest}"
                + (f"; release-note signals: {', '.join(note_risks)}" if note_risks else "")
            )
        if args.apply_updates and baseline["returncode"] == 0:
            version_path.write_text(latest + "\n", encoding="utf-8")
            update_readme_hugo_version(latest, readme_path)
            target = "safe_changes" if safe_hugo else "review_changes"
            data[target].append(f"Hugo {current} -> {latest}")

    data["theme"] = theme_dependency_summary()
    theme_updates = data["theme"].get("updates", [])
    risky_theme = [item for item in theme_updates if item.get("risk") in {"medium", "high"}]
    if risky_theme:
        data["manual_review_required"] = True
        data["manual_review_reasons"].append(
            "Mana theme dependency upgrades require review: "
            + ", ".join(item["name"] for item in risky_theme)
        )
    if args.apply_updates and baseline["returncode"] == 0 and theme_updates:
        result = apply_theme_candidates(theme_updates)
        data["validation"].append(result)
        if result.get("returncode") != 0:
            data["manual_review_required"] = True
            data["manual_review_reasons"].append("theme dependency update command failed")
            restore(package_path, originals[package_path])
            restore(lock_path, originals[lock_path])
        else:
            target = "review_changes" if risky_theme else "safe_changes"
            data[target].append("Mana theme dependency updates")

    if any(check.get("returncode") != 0 for check in data["validation"]):
        for path, original in originals.items():
            restore(path, original)
        data["safe_changes"] = []
        data["review_changes"] = []
        data["rollback"] = "candidate files were restored after validation failed"

    report_path = write_report(data)
    print(json.dumps({"report": str(report_path.relative_to(ROOT)), **data}, ensure_ascii=False, indent=2))
    return 0 if all(check.get("returncode") == 0 for check in data["validation"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
