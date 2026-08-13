#!/usr/bin/env python3
"""Reject unexpected mutations from the infrastructure maintenance workflow."""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
ALLOWED = {
    ".hugo-version",
    "README.md",
    "themes/mana/package.json",
    "themes/mana/package-lock.json",
}
ALLOWED_PREFIXES = (".maintenance/reports/",)


def changed_paths() -> list[str]:
    result = subprocess.run(
        ["git", "status", "--porcelain=v1", "--untracked-files=all"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=True,
    )
    paths: list[str] = []
    for line in result.stdout.splitlines():
        if len(line) < 4:
            continue
        value = line[3:].replace("\\", "/")
        paths.extend(value.split(" -> "))
    return paths


def allowed(path: str) -> bool:
    return path in ALLOWED or any(path.startswith(prefix) for prefix in ALLOWED_PREFIXES)


def readme_version_issues(root: Path = ROOT) -> list[str]:
    expected = (root / ".hugo-version").read_text(encoding="utf-8").strip()
    readme = (root / "README.md").read_text(encoding="utf-8")
    badge = re.findall(r"Hugo-v(\d+\.\d+\.\d+)-", readme)
    framework = re.findall(
        r"\*\*Framework:\*\* \[Hugo\]\(https://gohugo\.io/\) v(\d+\.\d+\.\d+)",
        readme,
    )
    issues: list[str] = []
    if badge != [expected]:
        issues.append(f"README Hugo badge must be {expected}; found {badge or 'none'}")
    if framework != [expected]:
        issues.append(f"README Tech Stack Hugo version must be {expected}; found {framework or 'none'}")
    return issues


def main() -> int:
    unexpected = [path for path in changed_paths() if not allowed(path)]
    version_issues = readme_version_issues()
    if unexpected or version_issues:
        print("Infrastructure validation failed:", file=sys.stderr)
        for path in unexpected:
            print(f"  {path}", file=sys.stderr)
        for issue in version_issues:
            print(f"  {issue}", file=sys.stderr)
        return 1
    print("Infrastructure changes are restricted to Hugo, Mana theme, and maintenance reports.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
