"""Synchronize the repository's pinned Hugo version with Cloudflare Pages."""

from __future__ import annotations

import json
import os
import re
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
API_ROOT = "https://api.cloudflare.com/client/v4"


def api_request(url: str, token: str, method: str = "GET", payload: dict[str, Any] | None = None) -> dict[str, Any]:
    body = None
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
    }
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    with urllib.request.urlopen(request, timeout=30) as response:
        result = json.load(response)
    if not result.get("success"):
        errors = result.get("errors") or []
        messages = ", ".join(str(error.get("message", error)) for error in errors)
        raise RuntimeError(messages or "Cloudflare API request failed")
    return result


def main() -> int:
    token = os.environ.get("CLOUDFLARE_API_TOKEN", "").strip()
    account_id = os.environ.get("CLOUDFLARE_ACCOUNT_ID", "").strip()
    project = os.environ.get("CLOUDFLARE_PAGES_PROJECT", "").strip()
    if not token or not account_id or not project:
        print("Cloudflare credentials are not configured; Hugo version synchronization skipped.")
        return 0

    version = (ROOT / ".hugo-version").read_text(encoding="utf-8").strip()
    if not re.fullmatch(r"\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?", version):
        raise RuntimeError(f"Invalid .hugo-version value: {version!r}")

    project_url = (
        f"{API_ROOT}/accounts/{urllib.parse.quote(account_id, safe='')}/pages/projects/"
        f"{urllib.parse.quote(project, safe='')}"
    )
    current = api_request(project_url, token)
    project_config = current.get("result") or {}
    deployment_configs = project_config.get("deployment_configs") or {}
    updated_configs: dict[str, dict[str, Any]] = {}
    changed = False

    for environment in ("production", "preview"):
        existing = deployment_configs.get(environment) or {}
        environment_vars = dict(existing.get("env_vars") or {})
        previous = environment_vars.get("HUGO_VERSION")
        previous_value = previous.get("value") if isinstance(previous, dict) else previous
        if previous_value != version:
            changed = True
        environment_vars["HUGO_VERSION"] = {"type": "plain_text", "value": version}
        updated_configs[environment] = {"env_vars": environment_vars}

    if not changed:
        print(f"Cloudflare Pages already uses Hugo {version} in production and preview.")
        return 0

    api_request(project_url, token, method="PATCH", payload={"deployment_configs": updated_configs})
    print(f"Cloudflare Pages Hugo version synchronized to {version} in production and preview.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
