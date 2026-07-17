"""Discover and safely activate newer stable Gemini Flash models."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
AUTOPUBLISHER_DIR = ROOT / ".autopublisher"
CONFIG_PATH = AUTOPUBLISHER_DIR / "config.json"
MODEL_STATE_PATH = AUTOPUBLISHER_DIR / "model-state.json"
API_ROOT = "https://generativelanguage.googleapis.com/v1beta"


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def api_json(url: str, api_key: str, method: str = "GET", payload: dict[str, Any] | None = None) -> dict[str, Any]:
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {"Accept": "application/json", "x-goog-api-key": api_key}
    if payload is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")[:1000]
        raise RuntimeError(f"Gemini API returned HTTP {error.code}: {detail}") from error


def list_models(api_key: str) -> list[dict[str, Any]]:
    models: list[dict[str, Any]] = []
    page_token = ""
    while True:
        query = {"pageSize": "1000"}
        if page_token:
            query["pageToken"] = page_token
        url = f"{API_ROOT}/models?{urllib.parse.urlencode(query)}"
        response = api_json(url, api_key)
        models.extend(item for item in response.get("models", []) if isinstance(item, dict))
        page_token = str(response.get("nextPageToken", "") or "")
        if not page_token:
            return models


def stable_flash_candidate(model: dict[str, Any], settings: dict[str, Any]) -> tuple[str, tuple[int, ...]] | None:
    base_model_id = str(model.get("baseModelId", "")).strip()
    if not base_model_id:
        base_model_id = str(model.get("name", "")).removeprefix("models/").strip()
    if not base_model_id:
        return None

    methods = {str(method) for method in model.get("supportedGenerationMethods", []) or []}
    if "generateContent" not in methods:
        return None

    lowered = base_model_id.lower()
    if any(marker in lowered for marker in ("preview", "experimental", "latest", "-lite", "-pro", "-exp")):
        return None
    if settings.get("stable_only", True) and str(model.get("modelStage", "")).upper() not in {"", "STABLE"}:
        return None

    family = re.escape(str(settings.get("family", "flash")))
    match = re.fullmatch(rf"gemini-(\d+(?:\.\d+)+)-{family}", base_model_id)
    if not match:
        return None

    if int(model.get("inputTokenLimit", 0) or 0) < int(settings.get("min_input_token_limit", 32768)):
        return None
    if int(model.get("outputTokenLimit", 0) or 0) < int(settings.get("min_output_token_limit", 8192)):
        return None
    version = tuple(int(part) for part in match.group(1).split("."))
    return base_model_id, version


def model_version(model_name: str) -> tuple[int, ...] | None:
    match = re.fullmatch(r"gemini-(\d+(?:\.\d+)+)-flash", model_name.strip())
    if not match:
        return None
    return tuple(int(part) for part in match.group(1).split("."))


def extract_text(response: dict[str, Any]) -> str:
    parts: list[str] = []
    for candidate in response.get("candidates", []) or []:
        for part in candidate.get("content", {}).get("parts", []) or []:
            if part.get("text"):
                parts.append(str(part["text"]))
    return "".join(parts).strip()


def smoke_test(api_key: str, model: str) -> None:
    prompt = 'Return exactly this JSON object and no other text: {"ok": true}'
    response = api_json(
        f"{API_ROOT}/models/{urllib.parse.quote(model)}:generateContent",
        api_key,
        method="POST",
        payload={
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {
                "temperature": 0,
                "responseMimeType": "application/json",
                "maxOutputTokens": 32,
            },
        },
    )
    text = extract_text(response)
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as error:
        raise RuntimeError(f"Stable model smoke test returned invalid JSON: {text[:300]!r}") from error
    if parsed.get("ok") is not True:
        raise RuntimeError(f"Stable model smoke test returned an unexpected response: {text[:300]!r}")


def current_models(config: dict[str, Any], state: dict[str, Any]) -> dict[str, str]:
    active = state.get("active_models", {}) if isinstance(state, dict) else {}
    if not isinstance(active, dict):
        active = {}
    gemini = config.get("gemini", {})
    return {
        "text": str(active.get("text") or gemini.get("text_model", "gemini-3.5-flash")),
        "qa": str(active.get("qa") or gemini.get("qa_model", "gemini-3.5-flash")),
        "grounded": str(active.get("grounded") or gemini.get("grounded_research_model", "gemini-3.5-flash")),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Discover and smoke-test without writing model state.")
    args = parser.parse_args()

    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key:
        raise SystemExit("GEMINI_API_KEY is required for Gemini model maintenance.")

    explicit_overrides = [
        os.environ.get("GEMINI_TEXT_MODEL", "").strip(),
        os.environ.get("GEMINI_QA_MODEL", "").strip(),
        os.environ.get("GEMINI_GROUNDED_RESEARCH_MODEL", "").strip(),
    ]
    if any(explicit_overrides):
        print(json.dumps({"status": "skipped", "reason": "explicit GEMINI_*_MODEL override is configured"}))
        return 0

    config = read_json(CONFIG_PATH, {})
    settings = config.get("gemini", {}).get("model_upgrade", {})
    if not settings.get("enabled", True):
        print(json.dumps({"status": "skipped", "reason": "model upgrade is disabled"}))
        return 0

    state = read_json(MODEL_STATE_PATH, {})
    current = current_models(config, state)
    current_text_version = model_version(current["text"])
    if current_text_version is None:
        print(json.dumps({"status": "skipped", "reason": f"current model is not a stable Flash model: {current['text']}"}))
        return 0

    candidates = [stable_flash_candidate(model, settings) for model in list_models(api_key)]
    candidates = [candidate for candidate in candidates if candidate is not None]
    if not candidates:
        print(json.dumps({"status": "unchanged", "reason": "no compatible stable Flash model was listed"}))
        return 0

    candidate, candidate_version = max(candidates, key=lambda item: item[1])
    max_major_jump = int(settings.get("max_major_jump", 1))
    if candidate_version <= current_text_version:
        print(json.dumps({"status": "unchanged", "current": current["text"], "latest_stable": candidate}))
        return 0
    if candidate_version[0] - current_text_version[0] > max_major_jump:
        print(json.dumps({"status": "skipped", "reason": "candidate exceeds configured major-version jump", "candidate": candidate}))
        return 0

    smoke_test(api_key, candidate)
    if args.dry_run:
        print(json.dumps({"status": "ready", "current": current["text"], "candidate": candidate}))
        return 0

    new_state = {
        "schema_version": 1,
        "active_models": {"text": candidate, "qa": candidate, "grounded": candidate},
        "previous_models": current,
        "updated_at": utc_now(),
        "reason": "automatic stable Gemini Flash upgrade after model discovery and smoke test",
        "candidate_version": list(candidate_version),
    }
    write_json(MODEL_STATE_PATH, new_state)
    print(json.dumps({"status": "updated", "previous": current, "active": new_state["active_models"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
