#!/usr/bin/env python3
"""Shared pre-commit safety gate for all repository-writing workflows."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
AUTOPUBLISHER = ROOT / "tools" / "autopublisher" / "autopublisher.py"
PUBLISH_RESULT = ROOT / ".autopublisher" / "publish-result.json"
GATE_RESULT = ROOT / ".autopublisher" / "release-gate-result.json"


ALLOWED_PATHS: dict[str, tuple[str, ...]] = {
    "publish": (
        "content/posts/",
        ".autopublisher/state.json",
        ".autopublisher/logs/",
        ".autopublisher/publish-result.json",
    ),
    "maintenance": (
        "content/posts/",
        ".autopublisher/state.json",
        ".autopublisher/logs/",
    ),
    "revision": (
        "content/posts/",
        ".autopublisher/revision-state.json",
        ".autopublisher/logs/",
    ),
    "infrastructure": (
        ".hugo-version",
        "themes/mana/package-lock.json",
        ".autopublisher/reports/",
        ".autopublisher/logs/",
        "infra-result.json",
    ),
    "model": (".autopublisher/model-state.json",),
    "validate": (".autopublisher/logs/",),
}


def run(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, cwd=ROOT, text=True, capture_output=True)


def status_paths() -> list[str]:
    result = run(["git", "status", "--porcelain=v1", "--untracked-files=all"])
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "git status failed")
    paths: list[str] = []
    for line in result.stdout.splitlines():
        if len(line) < 4:
            continue
        value = line[3:]
        if " -> " in value:
            paths.extend(value.split(" -> "))
        else:
            paths.append(value)
    return paths


def path_allowed(path: str, allowed: tuple[str, ...]) -> bool:
    normalized = path.replace("\\", "/")
    return any(normalized == item.rstrip("/") or normalized.startswith(item) for item in allowed)


def write_result(mode: str, approved: bool, reason: str, paths: list[str]) -> None:
    GATE_RESULT.parent.mkdir(parents=True, exist_ok=True)
    GATE_RESULT.write_text(
        json.dumps(
            {"mode": mode, "approved": approved, "reason": reason, "changed_paths": paths},
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def fail(mode: str, reason: str, paths: list[str]) -> int:
    write_result(mode, False, reason, paths)
    print(f"release_gate: FAIL: {reason}", file=sys.stderr)
    for path in paths:
        print(f"  changed: {path}", file=sys.stderr)
    return 1


def run_validation(mode: str) -> tuple[bool, str]:
    diff_check = run(["git", "diff", "--check"])
    if diff_check.returncode != 0:
        print(diff_check.stdout, end="")
        print(diff_check.stderr, end="", file=sys.stderr)
        return False, "git diff --check failed"

    if mode == "model":
        return True, "model state passed path validation"

    audit = run([sys.executable, str(AUTOPUBLISHER), "--mode", "audit"])
    print(audit.stdout, end="")
    if audit.returncode != 0:
        print(audit.stderr, end="", file=sys.stderr)
        return False, "content and asset audit failed"

    hugo = shutil.which("hugo")
    if not hugo:
        return False, "Hugo is not installed on PATH"
    build = run([hugo, "--minify"])
    print(build.stdout, end="")
    if build.returncode != 0:
        print(build.stderr, end="", file=sys.stderr)
        return False, "Hugo build failed"
    return True, "content audit and Hugo build passed"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=sorted(ALLOWED_PATHS), required=True)
    args = parser.parse_args()
    mode = str(args.mode)

    paths_before = status_paths()
    allowed = ALLOWED_PATHS[mode] + (".autopublisher/release-gate-result.json",)
    unexpected = [path for path in paths_before if not path_allowed(path, allowed)]
    if unexpected:
        return fail(mode, "unexpected files changed", unexpected)

    if mode == "publish":
        marker = json.loads(PUBLISH_RESULT.read_text(encoding="utf-8")) if PUBLISH_RESULT.exists() else {}
        result = marker.get("result")
        content_changed = any(path.replace("\\", "/").startswith("content/posts/") for path in paths_before)
        if result == "published" and not content_changed:
            return fail(mode, "publisher reported published but no content changed", paths_before)
        if result != "published" and content_changed:
            return fail(mode, "content changed without an approved publication", paths_before)
        if result != "published":
            write_result(mode, False, f"no approved publication: {result or 'missing result'}", paths_before)
            print(f"release_gate: no publication; result={result or 'missing'}")
            return 0

    valid, reason = run_validation(mode)
    paths_after = status_paths()
    unexpected_after = [path for path in paths_after if not path_allowed(path, allowed)]
    if unexpected_after:
        return fail(mode, "validation created unexpected files", unexpected_after)
    if not valid:
        return fail(mode, reason, paths_after)

    write_result(mode, True, reason, paths_after)
    print(f"release_gate: PASS: {reason}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
