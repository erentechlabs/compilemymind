"""Preflight the configured text-generation provider without failing scheduled work."""

from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path
from typing import Any

import autopublisher


def compact_reason(error: BaseException) -> str:
    """Produce a safe, single-line diagnostic suitable for logs and outputs."""
    message = re.sub(r"\s+", " ", str(error)).strip()
    return message[:300] or type(error).__name__


def check_provider(config: dict[str, Any] | None = None) -> dict[str, str]:
    """Return provider health while treating external unavailability as deferrable."""
    client = autopublisher.GeminiClient(
        config or autopublisher.load_config(),
        autopublisher.EventLog(),
    )
    task = "provider_health"
    if client._use_openai(task):
        provider, model = "openai", client.openai_model
    elif client._use_lightweight_model(task):
        provider, model = "github_models", client.github_models_model
    elif client.api_key:
        provider, model = "gemini", client.text_model
    else:
        return {
            "available": "true",
            "online": "false",
            "provider": "offline",
            "model": "deterministic",
            "reason": "no generation-provider credential is configured; deterministic mode selected",
        }

    try:
        response = client.generate_json(
            'Return exactly this JSON object: {"ok": true}',
            task=task,
            temperature=0,
            max_output_tokens=32,
        )
        if response.get("ok") is not True:
            raise RuntimeError("provider returned an unexpected health-check response")
    except Exception as error:
        return {
            "available": "true",
            "online": "false",
            "provider": "offline",
            "model": "deterministic",
            "reason": f"{provider}/{model} unavailable: {compact_reason(error)}",
        }
    return {
        "available": "true",
        "online": "true",
        "provider": provider,
        "model": model,
        "reason": "healthy",
    }


def write_github_outputs(path: Path, result: dict[str, str]) -> None:
    with path.open("a", encoding="utf-8", newline="\n") as output:
        for key in ("available", "online", "provider", "model"):
            value = result[key].replace("\r", " ").replace("\n", " ")
            output.write(f"{key}={value}\n")


def enable_offline_mode(path: Path) -> None:
    with path.open("a", encoding="utf-8", newline="\n") as output:
        output.write("AUTOPUBLISHER_FORCE_OFFLINE=true\n")


def github_annotation_escape(value: str) -> str:
    return value.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--github-output",
        type=Path,
        default=Path(os.environ["GITHUB_OUTPUT"]) if os.environ.get("GITHUB_OUTPUT") else None,
        help="Optional GitHub Actions output file (defaults to GITHUB_OUTPUT).",
    )
    parser.add_argument(
        "--github-env",
        type=Path,
        default=Path(os.environ["GITHUB_ENV"]) if os.environ.get("GITHUB_ENV") else None,
        help="Optional GitHub Actions environment file (defaults to GITHUB_ENV).",
    )
    args = parser.parse_args()
    result = check_provider()
    print(json.dumps(result, ensure_ascii=False))
    if args.github_output:
        write_github_outputs(args.github_output, result)
    if result["online"] != "true":
        if args.github_env:
            enable_offline_mode(args.github_env)
        detail = github_annotation_escape(
            f"{result['provider']} / {result['model']}: {result['reason']}"
        )
        print(f"::warning title=Deterministic offline mode enabled::{detail}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
