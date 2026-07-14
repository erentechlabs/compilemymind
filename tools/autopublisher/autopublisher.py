#!/usr/bin/env python3
"""Autonomous Hugo publishing and maintenance engine for Compile My Mind.

The script is designed for GitHub Actions:
- publish: research current sources, generate one approved article, and write a
  Hugo page bundle.
- maintain: review older articles, check links, and update only when useful.
- audit: validate local configuration and content inventory without calling AI.

It uses only the Python standard library so Actions runs do not depend on a
package install step.
"""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import email.utils
import gzip
import html
import json
import math
import os
import random
import re
import shutil
import subprocess
import sys
import textwrap
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import zlib
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover - Python 3.9+ in Actions has zoneinfo.
    ZoneInfo = None  # type: ignore


ROOT = Path(__file__).resolve().parents[2]
AUTOPUBLISHER_DIR = ROOT / ".autopublisher"
CONFIG_PATH = AUTOPUBLISHER_DIR / "config.json"
STATE_PATH = AUTOPUBLISHER_DIR / "state.json"
MODEL_STATE_PATH = AUTOPUBLISHER_DIR / "model-state.json"
PUBLISH_RESULT_PATH = AUTOPUBLISHER_DIR / "publish-result.json"

STOP_WORDS = {
    "a",
    "about",
    "after",
    "all",
    "also",
    "an",
    "and",
    "are",
    "as",
    "at",
    "be",
    "because",
    "been",
    "but",
    "by",
    "can",
    "for",
    "from",
    "has",
    "have",
    "how",
    "in",
    "into",
    "is",
    "it",
    "its",
    "more",
    "not",
    "of",
    "on",
    "or",
    "over",
    "that",
    "the",
    "their",
    "this",
    "to",
    "use",
    "using",
    "vs",
    "what",
    "when",
    "why",
    "with",
    "you",
    "your",
}


class GeminiQuotaError(RuntimeError):
    """Raised when Gemini reports rate-limit or quota exhaustion."""


class GeminiTransientError(RuntimeError):
    """Raised when Gemini is temporarily overloaded or unavailable."""


class GitHubModelsQuotaError(GeminiQuotaError):
    """Raised when GitHub Models reports a rate or usage limit."""


class GitHubModelsTransientError(GeminiTransientError):
    """Raised when GitHub Models is temporarily unavailable."""


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def local_now(config: dict[str, Any]) -> dt.datetime:
    timezone = config.get("site", {}).get("timezone", "UTC")
    if ZoneInfo is not None:
        return utc_now().astimezone(ZoneInfo(timezone))
    return utc_now()


def iso_z(now: dt.datetime | None = None) -> str:
    value = now or utc_now()
    return value.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(path)


class EventLog:
    def __init__(self) -> None:
        log_dir = AUTOPUBLISHER_DIR / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        self.path = log_dir / f"{utc_now().date().isoformat()}.jsonl"

    def log(self, event: str, **fields: Any) -> None:
        payload = {"time": iso_z(), "event": event, **fields}
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
        printable = {k: v for k, v in payload.items() if k not in {"article", "markdown"}}
        print(json.dumps(printable, ensure_ascii=False))


def normalize_space(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def strip_html(value: str) -> str:
    value = re.sub(r"(?is)<(script|style).*?</\1>", " ", value)
    value = re.sub(r"(?s)<[^>]+>", " ", value)
    return normalize_space(html.unescape(value))


def slugify(value: str, max_length: int = 82) -> str:
    value = unicodedata.normalize("NFKD", value)
    value = value.encode("ascii", "ignore").decode("ascii")
    value = value.lower().replace("&", " and ")
    value = re.sub(r"[^a-z0-9]+", "-", value)
    value = re.sub(r"-{2,}", "-", value).strip("-")
    return value[:max_length].strip("-") or "untitled-post"


def safe_filename(value: str, default: str) -> str:
    raw_value = Path(str(value).strip()).name
    raw_default = Path(str(default).strip()).name
    extension = Path(raw_value).suffix.lower()
    if extension not in {".svg", ".png", ".jpg", ".jpeg", ".webp", ".gif"}:
        extension = Path(raw_default).suffix.lower() or ".svg"
    stem = slugify(Path(raw_value).stem, max_length=54) or slugify(Path(raw_default).stem, max_length=54) or "asset"
    return f"{stem}{extension}"


def tokenize(value: str) -> list[str]:
    tokens = re.findall(r"[a-zA-Z0-9][a-zA-Z0-9+#.\-]{1,}", value.lower())
    return [token.strip(".-") for token in tokens if token not in STOP_WORDS and len(token) > 1]


def cosine_similarity(left: str, right: str) -> float:
    a = Counter(tokenize(left))
    b = Counter(tokenize(right))
    if not a or not b:
        return 0.0
    dot = sum(a[token] * b.get(token, 0) for token in a)
    norm_a = math.sqrt(sum(value * value for value in a.values()))
    norm_b = math.sqrt(sum(value * value for value in b.values()))
    if not norm_a or not norm_b:
        return 0.0
    return dot / (norm_a * norm_b)


def jaccard_similarity(left: str, right: str) -> float:
    a = set(tokenize(left))
    b = set(tokenize(right))
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def parse_date(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    value = value.strip().strip('"')
    try:
        parsed = email.utils.parsedate_to_datetime(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.timezone.utc)
        return parsed
    except Exception:
        pass
    for candidate in (value, value.replace("Z", "+00:00")):
        try:
            parsed = dt.datetime.fromisoformat(candidate)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=dt.timezone.utc)
            return parsed
        except Exception:
            continue
    return None


def yaml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, list):
        return json.dumps(value, ensure_ascii=False)
    return json.dumps(str(value), ensure_ascii=False)


def parse_frontmatter_value(raw: str) -> Any:
    raw = raw.strip()
    if raw in {"true", "false"}:
        return raw == "true"
    if raw.startswith("["):
        try:
            return json.loads(raw)
        except Exception:
            return [part.strip().strip('"').strip("'") for part in raw.strip("[]").split(",") if part.strip()]
    if raw.startswith('"') or raw.startswith("'"):
        try:
            return json.loads(raw)
        except Exception:
            return raw.strip('"').strip("'")
    return raw


def split_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, text
    end_index = None
    for index in range(1, len(lines)):
        if lines[index].strip() == "---":
            end_index = index
            break
    if end_index is None:
        return {}, text
    frontmatter: dict[str, Any] = {}
    for line in lines[1:end_index]:
        if not line.strip() or line.lstrip().startswith("#") or ":" not in line:
            continue
        key, raw_value = line.split(":", 1)
        frontmatter[key.strip()] = parse_frontmatter_value(raw_value)
    body = "\n".join(lines[end_index + 1 :]).lstrip("\n")
    return frontmatter, body


def compose_markdown(frontmatter: dict[str, Any], body: str) -> str:
    preferred_order = [
        "title",
        "date",
        "lastmod",
        "description",
        "summary",
        "tags",
        "categories",
        "image",
        "author",
        "draft",
        "autonomous",
        "series",
        "series_part",
        "planned_next_parts",
        "last_reviewed",
    ]
    lines = ["---"]
    used: set[str] = set()
    for key in preferred_order:
        if key in frontmatter and frontmatter[key] not in (None, "", []):
            lines.append(f"{key}: {yaml_value(frontmatter[key])}")
            used.add(key)
    for key in sorted(frontmatter):
        if key not in used and frontmatter[key] not in (None, "", []):
            lines.append(f"{key}: {yaml_value(frontmatter[key])}")
    lines.append("---")
    lines.append("")
    lines.append(body.strip() + "\n")
    return "\n".join(lines)


@dataclass
class Post:
    path: Path
    slug: str
    title: str
    description: str
    date: str
    tags: list[str]
    categories: list[str]
    body: str
    frontmatter: dict[str, Any]

    @property
    def url_path(self) -> str:
        return f"/posts/{self.slug}/"

    @property
    def searchable_text(self) -> str:
        return f"{self.title}\n{self.description}\n{' '.join(self.tags)}\n{' '.join(self.categories)}\n{self.body}"


@dataclass
class ResearchItem:
    source: str
    title: str
    url: str
    summary: str
    published: str
    categories: list[str]
    score: float
    snippet: str = ""


def load_config() -> dict[str, Any]:
    if not CONFIG_PATH.exists():
        raise SystemExit(f"Missing config: {CONFIG_PATH}")
    return read_json(CONFIG_PATH, {})


def load_state() -> dict[str, Any]:
    return read_json(
        STATE_PATH,
        {"version": 1, "generated_posts": [], "maintenance_reviews": {}, "failures": [], "last_runs": {}},
    )


def load_model_state() -> dict[str, Any]:
    return read_json(MODEL_STATE_PATH, {})


def grounded_research_fallback_enabled(config: dict[str, Any]) -> bool:
    """Allow publishing to continue from trusted feeds when Search grounding is unavailable."""
    value = config.get("gemini", {}).get("grounded_research_fallback_to_feeds", True)
    if isinstance(value, str):
        return value.strip().lower() not in {"0", "false", "no", "off"}
    return bool(value)


def save_state(state: dict[str, Any]) -> None:
    write_json(STATE_PATH, state)


def record_publish_retryable(state: dict[str, Any], stage: str, error: Exception) -> None:
    details = {"time": iso_z(), "result": "retryable", "stage": stage}
    state["last_runs"]["publish"] = details
    save_state(state)
    write_publish_result("retryable", stage=stage, error=str(error))


def write_publish_result(result: str, **fields: Any) -> None:
    write_json(PUBLISH_RESULT_PATH, {"time": iso_z(), "result": result, **fields})


def load_posts(config: dict[str, Any]) -> list[Post]:
    content_dir = ROOT / config["site"].get("content_dir", "content/posts")
    posts: list[Post] = []
    for index_path in sorted(content_dir.glob("*/index.md")):
        text = index_path.read_text(encoding="utf-8")
        frontmatter, body = split_frontmatter(text)
        slug = index_path.parent.name
        posts.append(
            Post(
                path=index_path,
                slug=slug,
                title=str(frontmatter.get("title", slug.replace("-", " ").title())),
                description=str(frontmatter.get("description", "")),
                date=str(frontmatter.get("date", "")),
                tags=list(frontmatter.get("tags", []) or []),
                categories=list(frontmatter.get("categories", []) or []),
                body=body,
                frontmatter=frontmatter,
            )
        )
    return posts


def category_counts(posts: list[Post], config: dict[str, Any]) -> dict[str, int]:
    allowed = config.get("taxonomy", {}).get("balance_categories", [])
    counts = {category: 0 for category in allowed}
    for post in posts:
        seen = set(post.categories) | set(post.tags)
        for category in allowed:
            if category in seen:
                counts[category] += 1
    return counts


def target_category(posts: list[Post], config: dict[str, Any]) -> str:
    counts = category_counts(posts, config)
    if not counts:
        return "technology"
    return sorted(counts.items(), key=lambda item: (item[1], item[0]))[0][0]


def http_request(
    url: str,
    *,
    method: str = "GET",
    payload: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    timeout: int = 30,
    retries: int = 2,
) -> tuple[int, bytes, dict[str, str]]:
    data = None
    request_headers = {
        "User-Agent": "CompileMyMindAutopublisher/1.0 (+https://www.compilemymind.com/)",
        "Accept-Encoding": "gzip, deflate",
    }
    if headers:
        request_headers.update(headers)
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        request_headers.setdefault("Content-Type", "application/json")
    last_error: Exception | None = None
    for attempt in range(retries + 1):
        try:
            request = urllib.request.Request(url, data=data, headers=request_headers, method=method)
            with urllib.request.urlopen(request, timeout=timeout) as response:
                status = int(getattr(response, "status", 200))
                response_headers = {key.lower(): value for key, value in response.headers.items()}
                return status, decode_http_body(response.read(), response_headers), response_headers
        except urllib.error.HTTPError as error:
            response_headers = {key.lower(): value for key, value in error.headers.items()}
            body = decode_http_body(error.read(), response_headers)
            if error.code < 500 or attempt == retries:
                return int(error.code), body, response_headers
            last_error = error
        except Exception as error:
            last_error = error
            if attempt == retries:
                raise
        time.sleep(2**attempt + random.random())
    if last_error:
        raise last_error
    raise RuntimeError(f"Request failed: {url}")


def decode_http_body(body: bytes, headers: dict[str, str]) -> bytes:
    """Decode common HTTP content encodings before parsing feeds or pages."""
    encoding = str(headers.get("content-encoding", "")).lower()
    if "gzip" in encoding:
        return gzip.decompress(body)
    if "deflate" in encoding:
        try:
            return zlib.decompress(body)
        except zlib.error:
            return zlib.decompress(body, -zlib.MAX_WBITS)
    return body


class GeminiClient:
    def __init__(self, config: dict[str, Any], log: EventLog) -> None:
        self.config = config
        self.log = log
        self.api_key = os.environ.get("GEMINI_API_KEY", "").strip()
        github_models = config.get("github_models", {})
        self.github_models_enabled = bool(github_models.get("enabled", True))
        self.github_models_token = (
            os.environ.get("GITHUB_MODELS_TOKEN", "").strip()
            or os.environ.get("GITHUB_TOKEN", "").strip()
        )
        self.github_models_endpoint = str(
            github_models.get("endpoint", "https://models.github.ai/inference/chat/completions")
        ).strip()
        self.github_models_model = str(
            github_models.get("model", "openai/gpt-4o-mini")
        ).strip()
        self.github_models_tasks = {
            str(task).strip()
            for task in github_models.get("lightweight_tasks", ["topic_selection"])
            if str(task).strip()
        }
        self.github_models_max_output_tokens = int(github_models.get("max_output_tokens", 4096))
        gemini_config = config.get("gemini", {})
        model_state = load_model_state() if gemini_config.get("model_upgrade", {}).get("enabled", True) else {}
        active_models = model_state.get("active_models", {}) if isinstance(model_state, dict) else {}
        if not isinstance(active_models, dict):
            active_models = {}
        self.text_model = (
            os.environ.get("GEMINI_TEXT_MODEL", "").strip()
            or active_models.get("text")
            or gemini_config.get("text_model", "gemini-3.5-flash")
        )
        self.qa_model = (
            os.environ.get("GEMINI_QA_MODEL", "").strip()
            or active_models.get("qa")
            or gemini_config.get("qa_model", self.text_model)
        )
        self.grounded_model = (
            os.environ.get("GEMINI_GROUNDED_RESEARCH_MODEL", "").strip()
            or active_models.get("grounded")
            or gemini_config.get("grounded_research_model", self.text_model)
        )
        self.image_model = os.environ.get("GEMINI_IMAGE_MODEL", "").strip() or gemini_config.get(
            "image_model", "gemini-3.1-flash-image"
        )
        self.base_url = "https://generativelanguage.googleapis.com/v1beta"

    def require_key(self) -> None:
        if not self.api_key:
            raise SystemExit("GEMINI_API_KEY is required for autonomous publishing and maintenance.")

    def generate_json(
        self,
        prompt: str,
        *,
        model: str | None = None,
        temperature: float | None = None,
        max_output_tokens: int | None = None,
        task: str | None = None,
    ) -> dict[str, Any]:
        self.require_key()
        if self._use_lightweight_model(task):
            try:
                return self._github_models_generate_json(
                    prompt,
                    temperature=temperature,
                    max_output_tokens=max_output_tokens,
                    task=task or "",
                )
            except Exception as error:
                self.log.log(
                    "lightweight_model_fallback",
                    task=task or "unknown",
                    model=self.github_models_model,
                    error=str(error),
                )
        selected_model = model or self.text_model
        config = {
            "temperature": temperature
            if temperature is not None
            else float(self.config.get("gemini", {}).get("temperature", 0.55)),
            "responseMimeType": "application/json",
            "maxOutputTokens": max_output_tokens or int(self.config.get("gemini", {}).get("max_output_tokens", 32768)),
        }
        response = self._generate_content(selected_model, prompt, config)
        text = self._extract_text(response)
        return parse_model_json(text)

    def generate_text(
        self,
        prompt: str,
        *,
        model: str | None = None,
        temperature: float | None = None,
        max_output_tokens: int | None = None,
        task: str | None = None,
    ) -> str:
        self.require_key()
        if self._use_lightweight_model(task):
            try:
                return self._github_models_generate_text(
                    prompt,
                    temperature=temperature,
                    max_output_tokens=max_output_tokens,
                    task=task or "",
                )
            except Exception as error:
                self.log.log(
                    "lightweight_model_fallback",
                    task=task or "unknown",
                    model=self.github_models_model,
                    error=str(error),
                )
        selected_model = model or self.text_model
        config = {
            "temperature": temperature
            if temperature is not None
            else float(self.config.get("gemini", {}).get("temperature", 0.55)),
            "maxOutputTokens": max_output_tokens or 8192,
        }
        response = self._generate_content(selected_model, prompt, config)
        return self._extract_text(response)

    def _use_lightweight_model(self, task: str | None) -> bool:
        return bool(
            task
            and self.github_models_enabled
            and self.github_models_token
            and task in self.github_models_tasks
        )

    def _github_models_generate_json(
        self,
        prompt: str,
        *,
        temperature: float | None,
        max_output_tokens: int | None,
        task: str,
    ) -> dict[str, Any]:
        response = self._github_models_request(
            prompt,
            temperature=temperature,
            max_output_tokens=max_output_tokens,
            task=task,
            json_mode=True,
        )
        parsed = parse_model_json(self._extract_chat_text(response))
        self.log.log("lightweight_model_used", task=task, model=self.github_models_model)
        return parsed

    def _github_models_generate_text(
        self,
        prompt: str,
        *,
        temperature: float | None,
        max_output_tokens: int | None,
        task: str,
    ) -> str:
        text = self._extract_chat_text(
            self._github_models_request(
                prompt,
                temperature=temperature,
                max_output_tokens=max_output_tokens,
                task=task,
            )
        )
        self.log.log("lightweight_model_used", task=task, model=self.github_models_model)
        return text

    def _github_models_request(
        self,
        prompt: str,
        *,
        temperature: float | None,
        max_output_tokens: int | None,
        task: str,
        json_mode: bool = False,
    ) -> dict[str, Any]:
        payload = {
            "model": self.github_models_model,
            "messages": [
                {
                    "role": "system",
                    "content": "Return only the requested answer. When JSON is requested, return valid JSON only.",
                },
                {"role": "user", "content": prompt},
            ],
            "temperature": temperature
            if temperature is not None
            else float(self.config.get("gemini", {}).get("temperature", 0.55)),
            "max_tokens": min(
                max_output_tokens or self.github_models_max_output_tokens,
                self.github_models_max_output_tokens,
            ),
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        status, body, _headers = http_request(
            self.github_models_endpoint,
            method="POST",
            payload=payload,
            headers={
                "Authorization": f"Bearer {self.github_models_token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2026-03-10",
            },
            timeout=120,
            retries=4,
        )
        if status == 429:
            raise GitHubModelsQuotaError(
                f"GitHub Models quota exceeded for {self.github_models_model}: HTTP {status}: {body[:800]!r}"
            )
        if status in {500, 502, 503, 504}:
            raise GitHubModelsTransientError(
                f"GitHub Models temporarily unavailable: HTTP {status}: {body[:800]!r}"
            )
        if status >= 400:
            raise RuntimeError(f"GitHub Models request failed: HTTP {status}: {body[:800]!r}")
        response = json.loads(body.decode("utf-8"))
        return response

    @staticmethod
    def _extract_chat_text(response: dict[str, Any]) -> str:
        choices = response.get("choices", []) or []
        if not choices:
            raise RuntimeError(f"GitHub Models response did not contain choices: {json.dumps(response)[:800]}")
        message = choices[0].get("message", {}) or {}
        content = message.get("content", "")
        if isinstance(content, list):
            content = "\n".join(
                str(part.get("text", "")) for part in content if isinstance(part, dict) and part.get("text")
            )
        text = str(content).strip()
        if not text:
            raise RuntimeError(f"GitHub Models response did not contain text: {json.dumps(response)[:800]}")
        return text

    def grounded_research(self, prompt: str) -> dict[str, Any]:
        self.require_key()
        url = f"{self.base_url}/interactions"
        payload = {
            "model": self.grounded_model,
            "input": prompt,
            "tools": [{"type": "google_search"}],
        }
        status, body, _headers = http_request(
            url,
            method="POST",
            payload=payload,
            headers={"x-goog-api-key": self.api_key},
            timeout=90,
            retries=4,
        )
        if status >= 400:
            if status == 429:
                raise GeminiQuotaError(f"Gemini grounded research quota exceeded: HTTP {status}: {body[:500]!r}")
            if status in {500, 502, 503, 504}:
                raise GeminiTransientError(f"Gemini grounded research temporarily unavailable: HTTP {status}: {body[:500]!r}")
            raise RuntimeError(f"Gemini grounded research failed: HTTP {status}: {body[:500]!r}")
        response = json.loads(body.decode("utf-8"))
        text_parts: list[str] = []
        citations: list[dict[str, str]] = []
        for step in response.get("steps", []):
            if step.get("type") != "model_output":
                continue
            for block in step.get("content", []):
                if block.get("type") == "text":
                    text_parts.append(block.get("text", ""))
                    for annotation in block.get("annotations", []) or []:
                        if annotation.get("type") == "url_citation":
                            citations.append(
                                {
                                    "title": str(annotation.get("title", "")),
                                    "url": str(annotation.get("url", "")),
                                }
                            )
        return {"text": "\n\n".join(text_parts).strip(), "citations": dedupe_sources(citations)}

    def generate_image(self, prompt: str, output_path: Path) -> bool:
        self.require_key()
        config = {
            "temperature": 0.8,
            "responseModalities": ["TEXT", "IMAGE"],
            "responseFormat": {"image": {"aspectRatio": "16:9", "imageSize": "2K"}},
        }
        try:
            response = self._generate_content(self.image_model, prompt, config, timeout=180)
        except Exception as error:
            self.log.log("gemini_image_error", error=str(error))
            return False
        for candidate in response.get("candidates", []):
            for part in candidate.get("content", {}).get("parts", []):
                inline_data = part.get("inlineData") or part.get("inline_data")
                if inline_data and inline_data.get("data"):
                    output_path.write_bytes(base64.b64decode(inline_data["data"]))
                    return True
        self.log.log("gemini_image_no_inline_data", model=self.image_model)
        return False

    def _generate_content(
        self,
        model: str,
        prompt: str,
        generation_config: dict[str, Any],
        *,
        timeout: int = 120,
    ) -> dict[str, Any]:
        url = f"{self.base_url}/models/{urllib.parse.quote(model)}:generateContent"
        payload = {
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": generation_config,
        }
        status, body, _headers = http_request(
            url,
            method="POST",
            payload=payload,
            headers={"x-goog-api-key": self.api_key},
            timeout=timeout,
            retries=4,
        )
        if status >= 400:
            if status == 429:
                raise GeminiQuotaError(f"Gemini quota exceeded for model {model}: HTTP {status}: {body[:1000]!r}")
            if status in {500, 502, 503, 504}:
                raise GeminiTransientError(f"Gemini generateContent temporarily unavailable: HTTP {status}: {body[:1000]!r}")
            raise RuntimeError(f"Gemini generateContent failed: HTTP {status}: {body[:1000]!r}")
        return json.loads(body.decode("utf-8"))

    @staticmethod
    def _extract_text(response: dict[str, Any]) -> str:
        parts: list[str] = []
        for candidate in response.get("candidates", []):
            for part in candidate.get("content", {}).get("parts", []):
                if "text" in part:
                    parts.append(part["text"])
        text = "\n".join(parts).strip()
        if not text:
            raise RuntimeError(f"Gemini response did not contain text: {json.dumps(response)[:800]}")
        return text


def parse_model_json(text: str) -> dict[str, Any]:
    cleaned = text.strip()
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
    cleaned = re.sub(r"\s*```$", "", cleaned)
    try:
        parsed = json.loads(cleaned)
        if isinstance(parsed, dict):
            return parsed
        if isinstance(parsed, list):
            return {"items": parsed}
        raise ValueError(f"Gemini JSON root must be an object or array, got {type(parsed).__name__}")
    except json.JSONDecodeError:
        pass
    decoder = json.JSONDecoder()
    starts = [index for index, char in enumerate(cleaned) if char in "{["]
    for start in starts:
        try:
            parsed, _end = decoder.raw_decode(cleaned[start:])
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
        if isinstance(parsed, list):
            return {"items": parsed}
    raise ValueError(f"Gemini response did not contain a JSON object: {cleaned[:500]}")


def xml_children(element: ET.Element, local_name: str) -> list[ET.Element]:
    return [child for child in element.iter() if child.tag.split("}")[-1] == local_name]


def first_text(element: ET.Element, *names: str) -> str:
    for name in names:
        for child in element:
            if child.tag.split("}")[-1] == name:
                if name == "link" and "href" in child.attrib:
                    return child.attrib.get("href", "")
                return "".join(child.itertext()).strip()
    return ""


def fetch_feed(source: dict[str, Any], config: dict[str, Any], log: EventLog) -> list[ResearchItem]:
    url = source["url"]
    try:
        status, body, _headers = http_request(
            url,
            headers={"Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, */*"},
            timeout=25,
            retries=1,
        )
        if status >= 400:
            raise RuntimeError(f"HTTP {status}")
        if not body.strip():
            raise RuntimeError("empty feed response")
        root = ET.fromstring(body)
    except Exception as error:
        log.log("research_source_failed", source=source.get("name"), url=url, error=str(error))
        return []

    items: list[ET.Element] = xml_children(root, "item")
    if not items:
        items = xml_children(root, "entry")

    results: list[ResearchItem] = []
    item_limit = int(config.get("research", {}).get("items_per_source", 6))
    for element in items[:item_limit]:
        title = normalize_space(first_text(element, "title"))
        link = normalize_space(first_text(element, "link", "guid", "id"))
        summary = strip_html(first_text(element, "description", "summary", "content"))
        published_raw = first_text(element, "pubDate", "published", "updated", "dc:date")
        published = parse_date(published_raw)
        if not title or not link:
            continue
        if not urllib.parse.urlparse(link).scheme:
            link = urllib.parse.urljoin(url, link)
        score = score_research_item(title, summary, published, source, config)
        results.append(
            ResearchItem(
                source=str(source.get("name", "")),
                title=title,
                url=link,
                summary=summary[:900],
                published=published.isoformat() if published else "",
                categories=list(source.get("categories", [])),
                score=score,
            )
        )
    return results


def score_research_item(
    title: str,
    summary: str,
    published: dt.datetime | None,
    source: dict[str, Any],
    config: dict[str, Any],
) -> float:
    text = f"{title} {summary}".lower()
    score = float(source.get("weight", 1.0))
    if published:
        age_days = max(0.0, (utc_now() - published.astimezone(dt.timezone.utc)).total_seconds() / 86400)
        score += max(0.0, 2.0 - min(age_days, 30) / 15)
    terms = config.get("research", {}).get("high_interest_terms", [])
    score += sum(0.15 for term in terms if term.lower() in text)
    month = str(local_now(config).month)
    for focus in config.get("seasonal_focus", {}).get(month, []):
        if any(token in text for token in tokenize(focus)):
            score += 0.25
    return round(score, 4)


def collect_research(config: dict[str, Any], log: EventLog) -> list[ResearchItem]:
    items: list[ResearchItem] = []
    for source in config.get("research", {}).get("trusted_sources", []):
        items.extend(fetch_feed(source, config, log))
    deduped: dict[str, ResearchItem] = {}
    for item in sorted(items, key=lambda candidate: candidate.score, reverse=True):
        key = slugify(item.title, max_length=90)
        if key not in deduped:
            deduped[key] = item
    max_items = int(config.get("research", {}).get("max_items_for_model", 36))
    chosen = sorted(deduped.values(), key=lambda candidate: candidate.score, reverse=True)[:max_items]
    log.log("research_collected", count=len(chosen), sources=len(config.get("research", {}).get("trusted_sources", [])))
    return chosen


def fetch_page_snippet(url: str, characters: int, log: EventLog) -> str:
    try:
        status, body, headers = http_request(url, timeout=18, retries=1)
        if status >= 400:
            return ""
        content_type = headers.get("content-type", "")
        if "text" not in content_type and "html" not in content_type and not content_type.startswith("application/"):
            return ""
        text = body[:250000].decode("utf-8", errors="ignore")
        return strip_html(text)[:characters]
    except Exception as error:
        log.log("snippet_fetch_failed", url=url, error=str(error))
        return ""


def enrich_research_snippets(items: list[ResearchItem], config: dict[str, Any], log: EventLog) -> None:
    characters = int(config.get("research", {}).get("snippet_characters", 1400))
    for item in items[:12]:
        item.snippet = fetch_page_snippet(item.url, characters, log)


def research_for_prompt(items: list[ResearchItem]) -> list[dict[str, Any]]:
    payload: list[dict[str, Any]] = []
    for item in items:
        payload.append(
            {
                "source": item.source,
                "title": item.title,
                "url": item.url,
                "summary": item.summary,
                "published": item.published,
                "categories": item.categories,
                "score": item.score,
                "snippet": item.snippet[:1400],
            }
        )
    return payload


def research_items_for_topic(topic: dict[str, Any], research: list[ResearchItem], *, limit: int = 8) -> list[ResearchItem]:
    selected_urls = set(topic.get("source_urls", []) or [])
    source_items = [item for item in research if item.url in selected_urls]
    if len(source_items) >= limit:
        return source_items[:limit]
    topic_tokens = set(tokenize(str(topic.get("title", "")) + " " + " ".join(topic.get("tags", []) or [])))
    ranked = sorted(
        research,
        key=lambda item: len(topic_tokens & set(tokenize(item.title + " " + item.summary))) + item.score,
        reverse=True,
    )
    for item in ranked:
        if item not in source_items:
            source_items.append(item)
        if len(source_items) >= limit:
            break
    return source_items


def dedupe_sources(sources: list[dict[str, Any]]) -> list[dict[str, str]]:
    seen: set[str] = set()
    output: list[dict[str, str]] = []
    for source in sources:
        url = str(source.get("url", "")).strip()
        if not url or url in seen:
            continue
        seen.add(url)
        output.append({"title": str(source.get("title") or urllib.parse.urlparse(url).netloc), "url": url})
    return output


def select_internal_links(posts: list[Post], topic: dict[str, Any], config: dict[str, Any]) -> list[dict[str, str]]:
    topic_text = " ".join(
        [
            str(topic.get("title", "")),
            str(topic.get("search_intent", "")),
            " ".join(topic.get("tags", []) or []),
            " ".join(topic.get("categories", []) or []),
        ]
    )
    scored: list[tuple[float, Post]] = []
    for post in posts:
        score = cosine_similarity(topic_text, post.searchable_text[:5000])
        if set(topic.get("tags", []) or []) & set(post.tags):
            score += 0.15
        if set(topic.get("categories", []) or []) & set(post.categories):
            score += 0.1
        scored.append((score, post))
    limit = int(config.get("publishing", {}).get("prefer_internal_links", 4))
    return [
        {"title": post.title, "url": post.url_path}
        for score, post in sorted(scored, key=lambda item: item[0], reverse=True)[:limit]
        if score > 0.03
    ]


def max_existing_similarity(candidate_text: str, posts: list[Post]) -> dict[str, Any]:
    best = {"score": 0.0, "title_score": 0.0, "post": None}
    title = candidate_text.splitlines()[0] if candidate_text.strip() else candidate_text
    for post in posts:
        score = cosine_similarity(candidate_text, post.searchable_text)
        title_score = jaccard_similarity(title, post.title)
        if score > best["score"] or title_score > best["title_score"]:
            best = {"score": score, "title_score": title_score, "post": post}
    return best


def topic_selection_research_payload(research: list[ResearchItem], config: dict[str, Any]) -> list[dict[str, Any]]:
    """Build a compact research view that fits lightweight-model request limits."""
    research_config = config.get("research", {})
    limit = int(research_config.get("topic_selection_max_items", 12))
    summary_chars = int(research_config.get("topic_selection_summary_characters", 260))
    snippet_chars = int(research_config.get("topic_selection_snippet_characters", 180))
    ranked = sorted(research, key=lambda item: item.score, reverse=True)[:limit]
    return [
        {
            "source": item.source,
            "title": item.title,
            "url": item.url,
            "summary": normalize_space(item.summary)[:summary_chars],
            "published": item.published,
            "categories": item.categories,
            "score": item.score,
            "snippet": normalize_space(item.snippet)[:snippet_chars],
        }
        for item in ranked
    ]


def topic_selection_existing_posts(posts: list[Post], config: dict[str, Any]) -> list[dict[str, Any]]:
    """Keep duplicate context useful without sending every article body to Phi."""
    research_config = config.get("research", {})
    limit = int(research_config.get("topic_selection_max_existing_posts", 18))
    description_chars = int(research_config.get("topic_selection_description_characters", 160))
    return [
        {
            "title": post.title,
            "slug": post.slug,
            "categories": post.categories,
            "tags": post.tags[:6],
            "description": normalize_space(post.description)[:description_chars],
        }
        for post in posts[-limit:]
    ]


def topic_selection_prompt(
    research: list[ResearchItem],
    grounded_brief: dict[str, Any] | None,
    posts: list[Post],
    config: dict[str, Any],
) -> str:
    counts = category_counts(posts, config)
    underrepresented = target_category(posts, config)
    existing_posts = topic_selection_existing_posts(posts, config)
    month = str(local_now(config).month)
    seasonal = config.get("seasonal_focus", {}).get(month, [])
    grounded = {
        "text": normalize_space(str((grounded_brief or {}).get("text", "")))[:1400],
        "citations": [
            {
                "title": str(citation.get("title", ""))[:160],
                "url": str(citation.get("url", "")),
            }
            for citation in ((grounded_brief or {}).get("citations", []) or [])
            if isinstance(citation, dict) and citation.get("url")
        ][:6],
    }
    return f"""
You are the autonomous editor for Compile My Mind, a technical blog covering the full computer, software, and IT landscape: software engineering, current AI models and AI developer tools, programming languages and version releases (Java, C#, .NET, Python, TypeScript, JavaScript, Go, Rust, Kotlin, Swift, Objective-C, C/C++), compilers and runtimes, web development, mobile development, Apple platforms (iOS, iPadOS, macOS, SwiftUI, Xcode), Android (Android SDK, Jetpack Compose), cross-platform frameworks (React Native, Flutter, Expo), mobile architecture/testing/security, systems design, databases, data engineering, developer tools, open source, DevOps, containers, observability, operating systems, networking, IT operations, cloud services, hardware, cybersecurity, Microsoft cloud, certification, and practical systems knowledge.

Choose the strongest publishable article topic for the next autonomous post.

Hard requirements:
- Match one or more existing/allowed categories.
- Prefer the underrepresented category if it can produce a genuinely useful post: {underrepresented}.
- Avoid duplicates and near-duplicates of existing posts.
- Prioritize current, trustworthy, high-interest topics with durable search demand.
- Prefer topics that can be educational and comprehensive, not shallow news summaries.
- Treat AI engineering, programming languages and runtimes, compilers, distributed systems, system design, databases, developer experience, observability, open source, and emerging software tools as first-class editorial areas.
- Actively consider new language/runtime versions, AI model and API releases, Apple/iOS and Android platform changes, Swift/Kotlin releases, mobile frameworks, cloud service changes, developer tooling, certification updates, and practical implementation guides—not only security or Azure topics.
- Include both durable technical explainers and timely release-driven articles when reliable sources support them.
- Consider seasonal focus: {json.dumps(seasonal, ensure_ascii=False)}.
- Create a multi-part series only when the topic naturally benefits from it.
- Return exactly 8 candidate topics, ranked best to worst.
- Include backup candidates across at least 4 different categories so the automation can continue if the first idea is rejected as too similar.
- Do not return only Azure fundamentals, IaaS basics, or certification-summary topics when similar existing Azure/certification articles already exist.

Category counts:
{json.dumps(counts, ensure_ascii=False, indent=2)}

Allowed categories:
{json.dumps(config.get("taxonomy", {}).get("allowed_categories", []), ensure_ascii=False)}

Existing posts:
{json.dumps(existing_posts, ensure_ascii=False, indent=2)}

Research feed items:
{json.dumps(topic_selection_research_payload(research, config), ensure_ascii=False, indent=2)}

Optional Gemini grounded research brief:
{json.dumps(grounded, ensure_ascii=False, indent=2)}

Return JSON only with this shape:
{{
  "topics": [
    {{
      "title": "SEO-friendly title",
      "slug": "url-slug",
      "primary_category": "allowed-category",
      "categories": ["guide", "allowed-category"],
      "tags": ["tag-one", "tag-two"],
      "search_intent": "what readers are trying to learn",
      "why_now": "why this topic is timely or evergreen",
      "source_urls": ["https://..."],
      "needs_diagram": true,
      "needs_chart": false,
      "series": {{
        "name": "",
        "part": null,
        "total_estimate": null,
        "planned_next_parts": []
      }}
    }}
  ],
  "editorial_reasoning": "short reasoning"
}}
""".strip()


def choose_topic(
    client: GeminiClient,
    research: list[ResearchItem],
    grounded_brief: dict[str, Any] | None,
    posts: list[Post],
    config: dict[str, Any],
    log: EventLog,
) -> dict[str, Any] | None:
    result = client.generate_json(
        topic_selection_prompt(research, grounded_brief, posts, config),
        temperature=float(config.get("github_models", {}).get("topic_selection_temperature", 0.1)),
        max_output_tokens=int(config.get("github_models", {}).get("topic_selection_max_output_tokens", 2400)),
        task="topic_selection",
    )
    candidates = result.get("topics") or []
    if isinstance(result.get("topic"), dict):
        candidates.insert(0, result["topic"])
    if isinstance(result.get("items"), list):
        candidates.extend(item for item in result["items"] if isinstance(item, dict))
    existing_slugs = {post.slug for post in posts}
    max_similarity = float(config.get("publishing", {}).get("max_similarity", 0.42))
    max_title_similarity = float(config.get("publishing", {}).get("max_title_similarity", 0.55))
    allowed = set(config.get("taxonomy", {}).get("allowed_categories", []))
    for topic in candidates:
        title = normalize_space(str(topic.get("title", "")))
        if not title:
            continue
        slug = slugify(str(topic.get("slug") or title))
        topic["slug"] = slug
        categories = sanitize_categories(topic.get("categories", []), topic.get("primary_category"), config)
        topic["categories"] = categories
        topic["tags"] = sanitize_tags(topic.get("tags", []), topic)
        if slug in existing_slugs:
            log.log("topic_rejected", title=title, reason="slug_exists", slug=slug)
            continue
        if not set(categories) & allowed:
            log.log("topic_rejected", title=title, reason="category_not_allowed", categories=categories)
            continue
        similarity = max_existing_similarity(
            f"{title}\n{topic.get('search_intent', '')}\n{' '.join(topic.get('tags', []) or [])}",
            posts,
        )
        similar_post = similarity["post"]
        if similarity["score"] > max_similarity or similarity["title_score"] > max_title_similarity:
            log.log(
                "topic_rejected",
                title=title,
                reason="too_similar",
                similarity=round(float(similarity["score"]), 4),
                title_similarity=round(float(similarity["title_score"]), 4),
                similar_post=similar_post.title if similar_post else "",
            )
            continue
        log.log(
            "topic_selected",
            title=title,
            slug=slug,
            categories=categories,
            reasoning=result.get("editorial_reasoning", ""),
        )
        return topic
    fallback = fallback_topic_from_research(
        research,
        posts,
        config,
        log,
        max_similarity=max_similarity,
        max_title_similarity=max_title_similarity,
    )
    if fallback:
        return fallback
    log.log("no_topic_selected", candidate_count=len(candidates))
    return None


def fallback_topic_from_research(
    research: list[ResearchItem],
    posts: list[Post],
    config: dict[str, Any],
    log: EventLog,
    *,
    max_similarity: float,
    max_title_similarity: float,
) -> dict[str, Any] | None:
    existing_slugs = {post.slug for post in posts}
    target = target_category(posts, config)
    allowed = set(config.get("taxonomy", {}).get("allowed_categories", []))
    ranked = sorted(
        research,
        key=lambda item: (0.7 if target in item.categories else 0.0) + item.score,
        reverse=True,
    )
    for allow_roundups in (False, True):
        for item in ranked:
            if not allow_roundups and is_roundup_research_item(item):
                continue
            primary = next((category for category in item.categories if category in allowed and category != "guide"), None)
            primary = primary or (target if target in allowed else "technology")
            title = fallback_topic_title(item, primary)
            slug = slugify(title)
            if slug in existing_slugs:
                continue
            categories = sanitize_categories([primary, *item.categories], primary, config)
            tags = sanitize_tags([primary, item.source, *tokenize(item.title)[:5]], {"title": title})
            similarity = max_existing_similarity(f"{title}\n{item.summary}\n{' '.join(tags)}", posts)
            similar_post = similarity["post"]
            if similarity["score"] > max_similarity or similarity["title_score"] > max_title_similarity:
                log.log(
                    "fallback_topic_rejected",
                    title=title,
                    reason="too_similar",
                    similarity=round(float(similarity["score"]), 4),
                    title_similarity=round(float(similarity["title_score"]), 4),
                    similar_post=similar_post.title if similar_post else "",
                )
                continue
            topic = {
                "title": title,
                "slug": slug,
                "primary_category": primary,
                "categories": categories,
                "tags": tags,
                "search_intent": f"Readers want a practical explanation of {item.title} and what it changes for technical teams.",
                "why_now": f"Based on a recent item from {item.source}: {item.title}",
                "source_urls": [item.url],
                "needs_diagram": primary in {"software-engineering", "ai-engineering", "programming-languages", "mobile-development", "systems-design", "developer-tools", "networking", "cybersecurity", "microsoft-cloud", "cloud"},
                "needs_chart": bool(re.search(r"(?i)\b(cost|price|benchmark|performance|percent|growth|compare|comparison)\b", item.title + " " + item.summary)),
                "series": {"name": "", "part": None, "total_estimate": None, "planned_next_parts": []},
            }
            log.log("fallback_topic_selected", title=title, slug=slug, categories=categories, source=item.source)
            return topic
    return None


def is_roundup_research_item(item: ResearchItem) -> bool:
    text = f"{item.title} {item.summary}"
    return bool(re.search(r"(?i)\b(what['\u2019]?s new|weekly|roundup|release notes|changelog|this week|newsletter)\b", text))


def fallback_topic_title(item: ResearchItem, primary_category: str) -> str:
    base = re.sub(r"\s*[-|]\s*(Google Cloud|AWS|Microsoft|GitHub|Cloudflare).*$", "", item.title).strip()
    base = base.rstrip(".")
    if primary_category == "cybersecurity":
        return f"{base}: Practical Security Lessons for IT Teams"
    if primary_category == "networking":
        return f"{base}: What It Means for Modern Network Operations"
    if primary_category in {"cloud", "microsoft-cloud"}:
        return f"{base}: Practical Cloud Architecture Guide"
    if primary_category in {"software-engineering", "ai-engineering", "programming-languages", "systems-design", "developer-tools"}:
        return f"{base}: Practical Guide for Developers"
    if primary_category == "hardware":
        return f"{base}: Practical Hardware Buying and Performance Guide"
    return f"{base}: Practical Guide and Real-World Examples"


def sanitize_categories(values: Any, primary: Any, config: dict[str, Any]) -> list[str]:
    allowed = set(config.get("taxonomy", {}).get("allowed_categories", []))
    output: list[str] = []
    raw_values = values if isinstance(values, list) else []
    if primary:
        raw_values = [primary, *raw_values]
    for value in raw_values:
        category = slugify(str(value), max_length=40)
        if category in allowed and category not in output:
            output.append(category)
    if not output:
        output = ["technology"]
    if "guide" not in output:
        output.insert(0, "guide")
    return output[:3]


def sanitize_tags(values: Any, topic: dict[str, Any]) -> list[str]:
    raw_values = values if isinstance(values, list) else []
    if not raw_values:
        raw_values = tokenize(str(topic.get("title", "")))[:6]
    tags: list[str] = []
    for value in raw_values:
        tag = slugify(str(value), max_length=34)
        if tag and tag not in tags:
            tags.append(tag)
    return tags[:8]


def metadata_enrichment_prompt(article: dict[str, Any], topic: dict[str, Any], config: dict[str, Any]) -> str:
    allowed_categories = config.get("taxonomy", {}).get("allowed_categories", [])
    return f"""
You are a lightweight editorial metadata assistant for Compile My Mind.

Do not rewrite the article. Read the existing title and body, then return concise,
accurate metadata that is faithful to the article. Do not invent facts, sources,
or categories. Use only the allowed categories.

Topic context:
{json.dumps({k: topic.get(k) for k in ["title", "categories", "tags", "search_intent"]}, ensure_ascii=False, indent=2)}

Current article metadata:
{json.dumps({k: article.get(k) for k in ["title", "description", "summary", "categories", "tags"]}, ensure_ascii=False, indent=2)}

Allowed categories:
{json.dumps(allowed_categories, ensure_ascii=False)}

Article body:
{str(article.get("article_markdown", ""))[:18000]}

Return JSON only:
{{
  "description": "105-180 character SEO description",
  "summary": "one concise, reader-facing article summary",
  "categories": ["guide", "one allowed technical category"],
  "tags": ["specific-topic-tag", "technology-tag"]
}}
""".strip()


def enrich_article_metadata(
    client: GeminiClient,
    article: dict[str, Any],
    topic: dict[str, Any],
    config: dict[str, Any],
    log: EventLog,
) -> dict[str, Any]:
    """Use the lightweight model for metadata without changing article prose."""
    metadata_topic = dict(topic)
    metadata_topic["title"] = article.get("title") or topic.get("title", "")
    metadata_topic["categories"] = article.get("categories") or topic.get("categories", [])
    metadata_topic["tags"] = article.get("tags") or topic.get("tags", [])
    try:
        payload = client.generate_json(
            metadata_enrichment_prompt(article, metadata_topic, config),
            temperature=0.2,
            max_output_tokens=1200,
            task="metadata_enrichment",
        )
    except Exception as error:
        # Metadata is helpful but must never turn a valid article into a failed run.
        log.log("metadata_enrichment_skipped", error=str(error))
        return article

    description = normalize_space(str(payload.get("description", "")))[:180]
    summary = normalize_space(str(payload.get("summary", "")))[:320]
    if len(description) >= 105:
        article["description"] = description
    elif len(str(article.get("description", ""))) < 105 and len(summary) >= 105:
        article["description"] = summary[:180]
    if summary:
        article["summary"] = summary
    if isinstance(payload.get("categories"), list) and payload["categories"]:
        article["categories"] = sanitize_categories(payload["categories"], None, config)
    if isinstance(payload.get("tags"), list) and payload["tags"]:
        article["tags"] = sanitize_tags(payload["tags"], metadata_topic)
    log.log(
        "metadata_enriched",
        fields=[field for field in ["description", "summary", "categories", "tags"] if field in article],
    )
    return article


def article_generation_prompt(
    topic: dict[str, Any],
    research: list[ResearchItem],
    posts: list[Post],
    config: dict[str, Any],
    feedback: str = "",
) -> str:
    source_items = research_items_for_topic(topic, research, limit=8)
    internal_links = select_internal_links(posts, topic, config)
    min_words = int(config.get("publishing", {}).get("min_words", 1400))
    required_sources = int(config.get("publishing", {}).get("required_source_count", 3))
    return f"""
You are writing for Compile My Mind. Create a comprehensive, original Hugo blog article as structured JSON.

Topic:
{json.dumps(topic, ensure_ascii=False, indent=2)}

Editorial style:
- Practical, technically accurate, readable, and educational.
- Explain concepts with concrete examples.
- Avoid hype, filler, and shallow news recap.
- Optimize for search intent naturally, with a clear meta description.
- Include at least one useful Markdown comparison or reference table when the subject supports it.
- Include internal links naturally, using only the exact Markdown URLs from the provided internal-link list.
- Do not create a Sources section inside article_markdown; the publishing system adds it automatically.
- Do not place external Markdown links, HTML links, autolinks, or bare external URLs inside article_markdown.
- Put every external citation only in the sources array.
- Every sources[].url must be copied exactly from one of the supplied research snippets. Do not invent, shorten, normalize, or add tracking parameters to URLs.
- Do not invent facts that are not supported by the research snippets.
- Do not include YAML front matter or any top-level H1 in article_markdown. Start section headings at H2 (##).
- Never refer to yourself, the prompt, or limitations of being an AI system.
- The sources array must include at least {required_sources} URLs selected from the research snippets.
- If the topic involves AI agents, code reviewers, or machine learning systems, discuss them as technical systems, not as yourself.
- For software topics, include runnable examples, architecture explanations, trade-offs, version context, and testing guidance when appropriate.
- The article body must contain at least {min_words} words; aim for {min_words + 250} to avoid falling below the minimum after normalization.
- Do not create a featured image, hero image, thumbnail, or image before the article title.
- Body diagrams, charts, and data visualizations are allowed only when they materially improve understanding; reference generated filenames in the Markdown.

Available internal links:
{json.dumps(internal_links, ensure_ascii=False, indent=2)}

Research snippets:
{json.dumps(research_for_prompt(source_items), ensure_ascii=False, indent=2)}

Previous QA feedback to fix:
{feedback or "No previous feedback. Produce the full article on the first attempt."}

Return JSON only with this shape:
{{
  "title": "final title",
  "slug": "final-slug",
  "description": "145-160 character SEO description",
  "categories": ["guide", "allowed-category"],
  "tags": ["tag-one"],
  "article_markdown": "Markdown body only, no front matter, no H1.",
  "diagrams": [
    {{
      "filename": "concept-flow.svg",
      "title": "Diagram title",
      "nodes": [{{"id": "a", "label": "First step"}}],
      "edges": [{{"from": "a", "to": "b", "label": "then"}}]
    }}
  ],
  "charts": [
    {{
      "filename": "comparison-chart.svg",
      "title": "Chart title",
      "unit": "percent",
      "data": [{{"label": "Option A", "value": 42}}]
    }}
  ],
  "sources": [
    {{"title": "Source title", "url": "https://..."}}
  ],
  "series_name": "",
  "series_part": null,
  "planned_next_parts": []
}}
""".strip()


def ensure_sources_section(markdown: str, sources: list[dict[str, Any]]) -> str:
    markdown = markdown.strip()
    if re.search(r"(?im)^##\s+Sources\b", markdown):
        markdown = re.sub(r"(?ims)^##\s+Sources\b.*$", "", markdown).rstrip()
    source_lines = ["## Sources", ""]
    for source in dedupe_sources(sources):
        title = source["title"] or urllib.parse.urlparse(source["url"]).netloc
        source_lines.append(f"- [{title}]({source['url']})")
    return markdown + "\n\n" + "\n".join(source_lines).strip() + "\n"


def _fence_character(line: str) -> str | None:
    match = re.match(r"^\s*(`{3,}|~{3,})", line)
    return match.group(1)[0] if match else None


def normalize_top_level_headings(markdown: str) -> str:
    """Convert prose H1 headings to H2 without touching fenced code."""
    lines: list[str] = []
    fence_character: str | None = None
    for line in markdown.splitlines():
        marker = _fence_character(line)
        if marker:
            if fence_character is None:
                fence_character = marker
            elif marker == fence_character:
                fence_character = None
            lines.append(line)
            continue
        if fence_character is None:
            line = re.sub(r"^\s*#\s+(.+?)\s*$", r"## \1", line)
        lines.append(line)
    return "\n".join(lines)


def markdown_without_fenced_code(markdown: str) -> str:
    """Return prose lines only for structural Markdown checks."""
    lines: list[str] = []
    fence_character: str | None = None
    for line in markdown.splitlines():
        marker = _fence_character(line)
        if marker:
            if fence_character is None:
                fence_character = marker
            elif marker == fence_character:
                fence_character = None
            continue
        if fence_character is None:
            lines.append(line)
    return "\n".join(lines)


def remove_accidental_frontmatter(markdown: str) -> str:
    frontmatter, body = split_frontmatter(markdown.strip())

    if frontmatter:
        markdown = body
    else:
        markdown = markdown.strip()

    # Hugo renders the page title from front matter. Convert prose H1 headings
    # to H2 without changing comments or examples inside fenced code blocks.
    return normalize_top_level_headings(markdown).strip()


def normalized_url_host(url: str) -> str:
    """Return a lowercase host without a leading www. for stable comparisons."""
    host = urllib.parse.urlsplit(str(url).strip()).netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    return host


def canonical_url(url: str) -> str:
    """Normalize a URL for trusted-source comparisons without changing stored URLs."""
    raw = str(url).strip()
    if not raw:
        return ""

    parsed = urllib.parse.urlsplit(raw)
    if parsed.scheme.lower() not in {"http", "https"}:
        return raw

    query_items = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    query_items = [
        (key, value)
        for key, value in query_items
        if not key.lower().startswith("utm_")
        and key.lower() not in {"fbclid", "gclid", "mc_cid", "mc_eid"}
    ]
    path = re.sub(r"/{2,}", "/", parsed.path or "/")
    if path != "/":
        path = path.rstrip("/")

    host = parsed.netloc.lower()
    if host.startswith("www."):
        host = host[4:]

    return urllib.parse.urlunsplit(
        (
            parsed.scheme.lower(),
            host,
            path,
            urllib.parse.urlencode(query_items, doseq=True),
            "",
        )
    )


def _sanitize_external_links_in_prose(markdown: str, site_base: str) -> str:
    """Remove external links from prose while preserving labels and internal links."""
    site_host = normalized_url_host(site_base)

    def is_internal(url: str) -> bool:
        parsed = urllib.parse.urlsplit(url)
        return bool(
            site_host
            and normalized_url_host(url) == site_host
            and parsed.scheme in {"http", "https"}
        )

    def replace_markdown_link(match: re.Match[str]) -> str:
        image_prefix, label, url, _optional_title = match.groups()
        if is_internal(url):
            return match.group(0)
        # Remote images are not trusted article assets. Preserve readable alt text only.
        if image_prefix:
            return f"*{label}*" if label.strip() else ""
        return label

    markdown = re.sub(
        r"(!?)\[([^\]]*)\]\((https?://[^)\s]+)(\s+[\"'][^\"']*[\"'])?\)",
        replace_markdown_link,
        markdown,
    )

    def replace_html_anchor(match: re.Match[str]) -> str:
        _before_href, _quote, url, _after_href, label = match.groups()
        if is_internal(url):
            return match.group(0)
        return strip_html(label)

    markdown = re.sub(
        r"(?is)<a\b([^>]*?href\s*=\s*)([\"'])(https?://[^\"']+)\2([^>]*)>(.*?)</a>",
        replace_html_anchor,
        markdown,
    )

    def replace_autolink(match: re.Match[str]) -> str:
        url = match.group(1)
        return match.group(0) if is_internal(url) else normalized_url_host(url)

    markdown = re.sub(r"<(https?://[^>\s]+)>", replace_autolink, markdown)

    def replace_bare_url(match: re.Match[str]) -> str:
        matched = match.group(0)
        url = matched.rstrip(".,;:")
        suffix = matched[len(url):]
        if is_internal(url):
            return matched
        return normalized_url_host(url) + suffix

    markdown = re.sub(
        r"(?<![\w@])(https?://[^\s)>\]\"']+)",
        replace_bare_url,
        markdown,
    )
    return markdown


def sanitize_article_external_links(markdown: str, site_base: str) -> str:
    """Sanitize prose links without modifying fenced code examples."""
    parts = re.split(r"(```.*?```|~~~.*?~~~)", markdown, flags=re.S)
    for index in range(0, len(parts), 2):
        parts[index] = _sanitize_external_links_in_prose(parts[index], site_base)
    return "".join(parts)


def ensure_asset_references(markdown: str, diagrams: list[dict[str, Any]], charts: list[dict[str, Any]]) -> str:
    missing: list[str] = []
    for item in diagrams:
        filename = item.get("filename")
        title = item.get("title") or "Diagram"
        if filename and filename not in markdown:
            missing.append(f"![{title}]({filename})")
    for item in charts:
        filename = item.get("filename")
        title = item.get("title") or "Chart"
        if filename and filename not in markdown:
            missing.append(f"![{title}]({filename})")
    if not missing:
        return markdown
    insertion = "## Visual Summary\n\n" + "\n\n".join(missing)
    sources_match = re.search(r"(?im)^##\s+Sources\b", markdown)
    if sources_match:
        return markdown[: sources_match.start()].rstrip() + "\n\n" + insertion + "\n\n" + markdown[sources_match.start() :].lstrip()
    return markdown.rstrip() + "\n\n" + insertion + "\n"


def default_diagram_for_topic(topic: dict[str, Any]) -> dict[str, Any]:
    title = normalize_space(str(topic.get("title") or "Concept flow"))
    return {
        "filename": "concept-flow.svg",
        "title": f"{title} flow",
        "nodes": [
            {"id": "context", "label": "Understand the context and trigger"},
            {"id": "risk", "label": "Identify the practical risks or trade-offs"},
            {"id": "controls", "label": "Apply the right technical controls"},
            {"id": "review", "label": "Review, monitor, and improve over time"},
        ],
        "edges": [
            {"from": "context", "to": "risk", "label": "leads to"},
            {"from": "risk", "to": "controls", "label": "mitigate with"},
            {"from": "controls", "to": "review", "label": "validate through"},
        ],
    }


def supplement_article_sources(
    article_sources: list[dict[str, Any]],
    topic: dict[str, Any],
    research: list[ResearchItem],
    config: dict[str, Any],
) -> list[dict[str, str]]:
    required = int(config.get("publishing", {}).get("required_source_count", 3))
    researched_urls = {item.url for item in research}
    sources = [
        source
        for source in (article_sources or [])
        if isinstance(source, dict)
        if str(source.get("url", "")).strip() in researched_urls
    ]
    sources.extend(
        {"url": url, "title": ""}
        for url in topic.get("source_urls", []) or []
        if str(url).strip() in researched_urls
    )
    for item in research_items_for_topic(topic, research, limit=8):
        sources.append({"title": item.title, "url": item.url})
        if len(dedupe_sources(sources)) >= required:
            break
    return dedupe_sources(sources)


def normalize_article_payload(article: dict[str, Any], topic: dict[str, Any], config: dict[str, Any], research: list[ResearchItem]) -> dict[str, Any]:
    title = normalize_space(str(article.get("title") or topic.get("title", "")))
    slug = slugify(str(article.get("slug") or topic.get("slug") or title))
    article["title"] = title
    article["slug"] = slug
    article["description"] = normalize_space(str(article.get("description", "")))[:180]
    article["categories"] = sanitize_categories(article.get("categories", topic.get("categories", [])), topic.get("primary_category"), config)
    article["tags"] = sanitize_tags(article.get("tags", topic.get("tags", [])), topic)
    article["sources"] = supplement_article_sources(article.get("sources", []), topic, research, config)
    article["diagrams"] = [
        item for item in article.get("diagrams", []) if isinstance(item, dict)
    ] if isinstance(article.get("diagrams"), list) else []
    if topic.get("needs_diagram") and not article["diagrams"]:
        article["diagrams"] = [default_diagram_for_topic(topic)]
    article["charts"] = [
        item for item in article.get("charts", []) if isinstance(item, dict)
    ] if isinstance(article.get("charts"), list) else []
    markdown = remove_accidental_frontmatter(str(article.get("article_markdown", "")))
    site_base = str(config.get("site", {}).get("base_url", "")).rstrip("/")
    markdown = sanitize_article_external_links(markdown, site_base)
    markdown = ensure_asset_references(markdown, article["diagrams"], article["charts"])
    article["article_markdown"] = ensure_sources_section(markdown, article["sources"])
    return article


def word_count(markdown: str) -> int:
    text = re.sub(r"```.*?```", " ", markdown, flags=re.S)
    text = re.sub(r"!\[[^\]]*\]\([^)]+\)", " ", text)
    return len(re.findall(r"\b[\w+#.-]+\b", text))


def markdown_format_issues(markdown: str) -> list[str]:
    """Catch structural Markdown errors before an article reaches the repository."""
    issues: list[str] = []
    if not markdown.strip():
        return ["Article body is empty."]
    prose = markdown_without_fenced_code(markdown)
    if re.search(r"(?m)^\s*#\s+\S", prose):
        issues.append("Article body contains a top-level H1; the Hugo template supplies the title.")
    if re.search(r"(?m)^\s*#{2,6}\s*$", prose):
        issues.append("Article contains an empty Markdown heading.")
    for marker, label in (("```", "backtick"), ("~~~", "tilde")):
        if len(re.findall(rf"(?m)^\s*{re.escape(marker)}", markdown)) % 2:
            issues.append(f"{label.title()} code fences are unbalanced.")
    if re.search(r"!\[[^\]]*\]\(\s*\)", prose):
        issues.append("Article contains an image reference with no target.")
    if re.search(r"(?m)^\s*[-*+]\s*$", prose):
        issues.append("Article contains an empty bullet-list item.")
    return issues


def deterministic_qa(
    article: dict[str, Any],
    topic: dict[str, Any],
    posts: list[Post],
    config: dict[str, Any],
) -> list[str]:
    issues: list[str] = []
    markdown = str(article.get("article_markdown", ""))
    issues.extend(markdown_format_issues(markdown))
    min_words = int(config.get("publishing", {}).get("min_words", 1400))
    if word_count(markdown) < min_words:
        issues.append(f"Article is too short: {word_count(markdown)} words, expected at least {min_words}.")
    if len(str(article.get("description", ""))) < 105:
        issues.append("SEO description is too short.")
    if config.get("publishing", {}).get("require_table", True) and "|" not in markdown:
        issues.append("Article should include at least one useful Markdown table.")
    if len(article.get("sources", []) or []) < int(config.get("publishing", {}).get("required_source_count", 3)):
        issues.append("Article does not include enough reliable sources.")
    if topic.get("needs_diagram") and not article.get("diagrams"):
        issues.append("Selected topic requested a diagram, but no diagram spec was returned.")
    if topic.get("needs_chart") and not article.get("charts") and not re.search(r"\|\s*[^|\n]+\s*\|", markdown):
        issues.append("Selected topic requested numerical comparison, but no chart or comparison table was returned.")
    trusted_urls = {
        canonical_url(str(source.get("url", "")).strip())
        for source in article.get("sources", []) or []
        if isinstance(source, dict) and source.get("url")
    }
    site_base = str(config.get("site", {}).get("base_url", "")).rstrip("/")
    site_host = normalized_url_host(site_base)
    untrusted_urls: list[str] = []
    for url in extract_links(markdown):
        is_internal = bool(site_host and normalized_url_host(url) == site_host)
        if not is_internal and canonical_url(url) not in trusted_urls:
            untrusted_urls.append(url)
    if untrusted_urls:
        displayed_urls = ", ".join(untrusted_urls[:5])
        issues.append(
            "Article contains external links that are not in its trusted source list: "
            f"{displayed_urls}"
        )
    if re.search(r"(?i)\bas an ai language model\b|\bas a language model\b|\bi (?:cannot|can't) (?:browse|access|provide|verify)\b", markdown):
        issues.append("Article contains AI self-reference.")
    similarity = max_existing_similarity(
        f"{article.get('title', '')}\n{article.get('description', '')}\n{markdown[:8000]}",
        posts,
    )
    max_similarity = float(config.get("publishing", {}).get("max_similarity", 0.42))
    max_title_similarity = float(config.get("publishing", {}).get("max_title_similarity", 0.55))
    if similarity["score"] > max_similarity:
        post = similarity["post"]
        issues.append(f"Article is too similar to existing post '{post.title if post else ''}' ({similarity['score']:.2f}).")
    if similarity["title_score"] > max_title_similarity:
        post = similarity["post"]
        issues.append(f"Title is too similar to existing post '{post.title if post else ''}' ({similarity['title_score']:.2f}).")
    return issues


def ai_qa(
    client: GeminiClient,
    article: dict[str, Any],
    topic: dict[str, Any],
    config: dict[str, Any],
    log: EventLog,
) -> dict[str, Any]:
    prompt = f"""
You are the final quality gate for Compile My Mind.

Review the article for:
- technical accuracy based only on cited/source material and broadly stable facts,
- originality and non-duplication,
- depth and educational value,
- SEO clarity,
- readable structure,
- appropriate use of examples, tables, diagrams, and charts,
- broken formatting or missing sources.

Reject if it is shallow, incomplete, unsupported, repetitive, or likely inaccurate.

Topic:
{json.dumps(topic, ensure_ascii=False, indent=2)}

Article payload:
{json.dumps({k: article.get(k) for k in ["title", "description", "categories", "tags", "sources", "diagrams", "charts", "article_markdown"]}, ensure_ascii=False, indent=2)[:24000]}

Return JSON only:
{{
  "approved": true,
  "score": 0.0,
  "issues": [],
  "required_fixes": [],
  "reason": "short explanation"
}}
""".strip()
    try:
        return client.generate_json(
            prompt,
            model=client.qa_model,
            temperature=float(config.get("gemini", {}).get("qa_temperature", 0.15)),
            max_output_tokens=8192,
            task="quality_assurance",
        )
    except (GeminiQuotaError, GeminiTransientError):
        raise
    except Exception as error:
        log.log("ai_qa_failed", error=str(error))
        return {
            "approved": False,
            "score": 0.0,
            "issues": ["AI QA was unavailable, so the article was rejected closed."],
            "required_fixes": ["Retry the article generation and quality review."],
            "reason": str(error),
        }


def svg_text_lines(label: str, max_chars: int) -> list[str]:
    label = normalize_space(str(label))
    return textwrap.wrap(label, width=max_chars, break_long_words=False)[:3] or [""]


def _svg_number(value: str | None, default: float) -> float:
    match = re.search(r"[-+]?\d*\.?\d+", str(value or ""))
    return float(match.group(0)) if match else default


def _svg_declarations(value: str) -> dict[str, str]:
    declarations: dict[str, str] = {}
    for declaration in value.split(";"):
        if ":" not in declaration:
            continue
        key, item = declaration.split(":", 1)
        declarations[key.strip().lower()] = item.strip()
    return declarations


def _svg_class_styles(root: ET.Element) -> dict[str, dict[str, str]]:
    styles: dict[str, dict[str, str]] = {}
    for element in root.iter():
        if str(element.tag).split("}")[-1] != "style":
            continue
        stylesheet = "".join(element.itertext())
        for match in re.finditer(r"\.([\w-]+)\s*\{([^}]*)\}", stylesheet):
            styles[match.group(1)] = _svg_declarations(match.group(2))
    return styles


def _svg_text_properties(element: ET.Element, class_styles: dict[str, dict[str, str]]) -> dict[str, str]:
    properties: dict[str, str] = {}
    for class_name in str(element.get("class", "")).split():
        properties.update(class_styles.get(class_name, {}))
    properties.update(_svg_declarations(element.get("style", "")))
    return properties


def _svg_font_size(element: ET.Element, properties: dict[str, str]) -> float:
    direct_size = element.get("font-size") or properties.get("font-size")
    if direct_size:
        return _svg_number(direct_size, 16.0)
    return _svg_number(properties.get("font"), 16.0)


def _svg_translation(value: str | None) -> tuple[float, float]:
    match = re.search(r"translate\(\s*([-+]?\d*\.?\d+)(?:[ ,]+([-+]?\d*\.?\d+))?", str(value or ""))
    if not match:
        return 0.0, 0.0
    return float(match.group(1)), float(match.group(2) or 0.0)


def svg_text_overlap_issues(path: Path) -> list[str]:
    """Return visible SVG text collisions using conservative text bounding boxes."""
    try:
        root = ET.fromstring(path.read_text(encoding="utf-8"))
    except (OSError, ET.ParseError) as error:
        return [f"{path.relative_to(ROOT)}: invalid SVG ({error})"]

    class_styles = _svg_class_styles(root)
    strict = root.get("data-text-collision-check") == "strict"
    boxes: list[tuple[str, float, float, float, float, float, float]] = []
    text_index = 0

    def visit(element: ET.Element, offset_x: float = 0.0, offset_y: float = 0.0) -> None:
        nonlocal text_index
        translate_x, translate_y = _svg_translation(element.get("transform"))
        offset_x += translate_x
        offset_y += translate_y
        if str(element.tag).split("}")[-1] == "text":
            value = normalize_space("".join(element.itertext()))
            if value:
                properties = _svg_text_properties(element, class_styles)
                font_size = _svg_font_size(element, properties)
                x = _svg_number(element.get("x"), 0.0) + offset_x
                y = _svg_number(element.get("y"), 0.0) + offset_y
                # Arial/Inter average glyph widths are below 0.6em; using 0.62em
                # makes this a useful safety check without rejecting adjacent labels.
                text_width = max(font_size * 0.62 * len(value), font_size * 0.5)
                anchor = element.get("text-anchor") or properties.get("text-anchor", "start")
                if anchor == "middle":
                    left = x - text_width / 2
                    right = x + text_width / 2
                elif anchor == "end":
                    left = x - text_width
                    right = x
                else:
                    left = x
                    right = x + text_width
                # Baseline-to-baseline spacing can be slightly less than 1em
                # without visible glyph collisions, so use a conservative glyph box.
                top = y - font_size * 0.8
                bottom = y + font_size * 0.2
                boxes.append((f"text[{text_index}]={value[:80]!r}", x, y, left, top, right, bottom))
                text_index += 1
        for child in element:
            visit(child, offset_x, offset_y)

    visit(root)

    issues: list[str] = []
    for index, first in enumerate(boxes):
        for second in boxes[index + 1 :]:
            _, x_a, y_a, left_a, top_a, right_a, bottom_a = first
            _, x_b, y_b, left_b, top_b, right_b, bottom_b = second
            same_position = math.isclose(x_a, x_b, abs_tol=0.01) and math.isclose(y_a, y_b, abs_tol=0.01)
            overlaps = left_a < right_b and right_a > left_b and top_a < bottom_b and bottom_a > top_b
            if same_position or (strict and overlaps):
                issues.append(
                    f"{path.relative_to(ROOT)}: overlapping SVG text ({first[0]} and {second[0]})"
                )
    return issues


def render_flowchart_svg(diagram: dict[str, Any], path: Path) -> None:
    nodes = diagram.get("nodes", []) or []
    edges = diagram.get("edges", []) or []
    if not nodes:
        nodes = [{"id": "a", "label": diagram.get("title", "Concept")}]
    width = 1200
    box_width = 760
    box_height = 82
    gap = 46
    title_height = 96
    height = title_height + len(nodes) * (box_height + gap) + 42
    x = (width - box_width) // 2
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}" role="img" aria-labelledby="title desc" data-text-collision-check="strict">',
        "<defs>",
        '<linearGradient id="bg" x1="0" x2="1" y1="0" y2="1"><stop offset="0" stop-color="#f8fafc"/><stop offset="1" stop-color="#eef2ff"/></linearGradient>',
        '<marker id="arrow" markerWidth="12" markerHeight="12" refX="6" refY="6" orient="auto"><path d="M2,2 L10,6 L2,10 z" fill="#475569"/></marker>',
        "</defs>",
        '<rect width="1200" height="100%" rx="28" fill="url(#bg)"/>',
        f'<title id="title">{html.escape(str(diagram.get("title", "Flowchart")))}</title>',
        f'<desc id="desc">A flowchart for {html.escape(str(diagram.get("title", "the article concept")))}</desc>',
        f'<text x="{width / 2}" y="48" text-anchor="middle" font-family="Inter, Arial, sans-serif" font-size="30" font-weight="700" fill="#0f172a">{html.escape(str(diagram.get("title", "Concept Flow")))}</text>',
    ]
    node_y: dict[str, int] = {}
    for index, node in enumerate(nodes):
        y = title_height + index * (box_height + gap)
        node_id = str(node.get("id", index))
        node_y[node_id] = y
        fill = "#ffffff" if index % 2 == 0 else "#f8fafc"
        parts.append(f'<rect x="{x}" y="{y}" width="{box_width}" height="{box_height}" rx="18" fill="{fill}" stroke="#cbd5e1" stroke-width="2"/>')
        parts.append(f'<circle cx="{x + 44}" cy="{y + box_height / 2}" r="22" fill="#2563eb"/>')
        parts.append(f'<text x="{x + 44}" y="{y + box_height / 2 + 7}" text-anchor="middle" font-family="Inter, Arial, sans-serif" font-size="20" font-weight="700" fill="#ffffff">{index + 1}</text>')
        for line_index, line in enumerate(svg_text_lines(str(node.get("label", "")), 52)):
            text_y = y + 32 + line_index * 22
            parts.append(f'<text x="{x + 86}" y="{text_y}" font-family="Inter, Arial, sans-serif" font-size="21" fill="#0f172a">{html.escape(line)}</text>')
        if index < len(nodes) - 1:
            x_mid = width // 2
            y1 = y + box_height + 8
            y2 = y + box_height + gap - 12
            parts.append(f'<line x1="{x_mid}" y1="{y1}" x2="{x_mid}" y2="{y2}" stroke="#475569" stroke-width="3" marker-end="url(#arrow)"/>')
    edge_labels: dict[str, list[str]] = defaultdict(list)
    for edge in edges:
        label = normalize_space(str(edge.get("label", "")))
        from_id = str(edge.get("from", ""))
        if label and from_id in node_y and label not in edge_labels[from_id]:
            edge_labels[from_id].append(label)
    for from_id, labels in edge_labels.items():
        y = node_y[from_id] + box_height + gap / 2
        combined_label = " • ".join(labels)
        lines = svg_text_lines(combined_label, 34)
        first_y = y + 5 - (len(lines) - 1) * 9
        for line_index, line in enumerate(lines):
            parts.append(
                f'<text x="{width / 2 + 26}" y="{first_y + line_index * 18}" '
                f'font-family="Inter, Arial, sans-serif" font-size="14" fill="#475569">{html.escape(line)}</text>'
            )
    parts.append("</svg>")
    path.write_text("\n".join(parts), encoding="utf-8")


def render_bar_chart_svg(chart: dict[str, Any], path: Path) -> None:
    data = chart.get("data", []) or []
    data = [item for item in data if isinstance(item, dict) and isinstance(item.get("value"), (int, float))]
    if not data:
        data = [{"label": "Value", "value": 1}]
    width = 1200
    row_height = 58
    top = 116
    height = top + len(data) * row_height + 70
    left = 250
    right = 1080
    max_value = max(float(item["value"]) for item in data) or 1.0
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}" role="img" aria-labelledby="title desc" data-text-collision-check="strict">',
        '<rect width="1200" height="100%" rx="28" fill="#f8fafc"/>',
        f'<title id="title">{html.escape(str(chart.get("title", "Comparison Chart")))}</title>',
        f'<desc id="desc">A horizontal bar chart for {html.escape(str(chart.get("title", "article data")))}</desc>',
        f'<text x="{width / 2}" y="50" text-anchor="middle" font-family="Inter, Arial, sans-serif" font-size="30" font-weight="700" fill="#0f172a">{html.escape(str(chart.get("title", "Comparison Chart")))}</text>',
        f'<text x="{width / 2}" y="82" text-anchor="middle" font-family="Inter, Arial, sans-serif" font-size="16" fill="#475569">{html.escape(str(chart.get("unit", "")))}</text>',
    ]
    for index, item in enumerate(data):
        y = top + index * row_height
        value = float(item["value"])
        bar_width = max(8, int((right - left) * value / max_value))
        label = html.escape(normalize_space(str(item.get("label", "")))[:32])
        parts.append(f'<text x="{left - 18}" y="{y + 28}" text-anchor="end" font-family="Inter, Arial, sans-serif" font-size="18" fill="#0f172a">{label}</text>')
        parts.append(f'<rect x="{left}" y="{y}" width="{right-left}" height="34" rx="10" fill="#e2e8f0"/>')
        color = "#2563eb" if index % 2 == 0 else "#0891b2"
        parts.append(f'<rect x="{left}" y="{y}" width="{bar_width}" height="34" rx="10" fill="{color}"/>')
        parts.append(f'<text x="{left + bar_width + 14}" y="{y + 24}" font-family="Inter, Arial, sans-serif" font-size="17" font-weight="700" fill="#0f172a">{value:g}</text>')
    parts.append("</svg>")
    path.write_text("\n".join(parts), encoding="utf-8")


def write_article_bundle(
    article: dict[str, Any],
    topic: dict[str, Any],
    config: dict[str, Any],
    log: EventLog,
    *,
    dry_run: bool,
) -> Path:
    content_dir = ROOT / config["site"].get("content_dir", "content/posts")
    slug = article["slug"]
    post_dir = content_dir / slug
    index_path = post_dir / "index.md"
    if dry_run:
        log.log("dry_run_article_ready", slug=slug, title=article["title"])
        return index_path
    post_dir.mkdir(parents=True, exist_ok=False)

    for diagram in article.get("diagrams", []) or []:
        filename = safe_filename(str(diagram.get("filename", "")), "diagram.svg")
        diagram["filename"] = filename
        diagram_path = post_dir / filename
        render_flowchart_svg(diagram, diagram_path)
        diagram_issues = svg_text_overlap_issues(diagram_path)
        if diagram_issues:
            raise ValueError("Generated diagram failed its text-collision check: " + "; ".join(diagram_issues))

    for chart in article.get("charts", []) or []:
        filename = safe_filename(str(chart.get("filename", "")), "chart.svg")
        chart["filename"] = filename
        chart_path = post_dir / filename
        render_bar_chart_svg(chart, chart_path)
        chart_issues = svg_text_overlap_issues(chart_path)
        if chart_issues:
            raise ValueError("Generated chart failed its text-collision check: " + "; ".join(chart_issues))

    now = local_now(config)
    frontmatter: dict[str, Any] = {
        "title": article["title"],
        "date": now.replace(microsecond=0).isoformat(),
        "description": article["description"],
        "tags": article["tags"],
        "categories": article["categories"],
        "author": config.get("site", {}).get("author", "Eren"),
        "draft": False,
        "autonomous": True,
    }
    if article.get("summary"):
        frontmatter["summary"] = normalize_space(str(article["summary"]))[:320]
    if article.get("series_name"):
        frontmatter["series"] = article.get("series_name")
    if article.get("series_part"):
        frontmatter["series_part"] = article.get("series_part")
    if article.get("planned_next_parts"):
        frontmatter["planned_next_parts"] = article.get("planned_next_parts")

    index_path.write_text(compose_markdown(frontmatter, article["article_markdown"]), encoding="utf-8")
    log.log("article_written", path=str(index_path.relative_to(ROOT)), title=article["title"], slug=slug)
    return index_path


def content_asset_issues(config: dict[str, Any]) -> list[str]:
    """Return broken local Markdown image references and forbidden featured images."""
    content_dir = ROOT / config["site"].get("content_dir", "content/posts")
    issues: list[str] = []
    image_pattern = re.compile(r"!\[[^\]]*\]\(\s*(?:<([^>]+)>|([^\s)]+))")
    for index_path in sorted(content_dir.glob("*/index.md")):
        frontmatter, body = split_frontmatter(index_path.read_text(encoding="utf-8"))
        if frontmatter.get("image"):
            issues.append(f"{index_path.relative_to(ROOT)}: featured image front matter is not allowed")
        post_root = index_path.parent.resolve()
        for match in image_pattern.finditer(body):
            reference = (match.group(1) or match.group(2) or "").strip()
            reference = reference.split("#", 1)[0].strip()
            if not reference or reference.startswith(("http://", "https://", "data:", "/")):
                continue
            target = (post_root / urllib.parse.unquote(reference)).resolve()
            try:
                target.relative_to(post_root)
            except ValueError:
                issues.append(f"{index_path.relative_to(ROOT)}: image escapes post bundle: {reference}")
                continue
            if not target.is_file():
                issues.append(f"{index_path.relative_to(ROOT)}: missing image asset: {reference}")
    for svg_path in sorted(content_dir.rglob("*.svg")):
        issues.extend(svg_text_overlap_issues(svg_path))
    return issues


def run_hugo_build(log: EventLog) -> bool:
    asset_issues = content_asset_issues(load_config())
    if asset_issues:
        log.log("content_asset_audit_failed", issues=asset_issues[:50], issue_count=len(asset_issues))
        return False
    hugo = shutil.which("hugo")
    if not hugo:
        log.log("hugo_missing", message="Hugo is not installed on PATH; workflow should install it before publishing.")
        return False
    result = subprocess.run([hugo, "--minify"], cwd=ROOT, text=True, capture_output=True, timeout=180)
    if result.returncode != 0:
        log.log("hugo_build_failed", stdout=result.stdout[-2000:], stderr=result.stderr[-4000:])
        return False
    log.log("hugo_build_passed", stdout=result.stdout[-1000:])
    return True


def run_publish(args: argparse.Namespace) -> int:
    config = load_config()
    state = load_state()
    log = EventLog()
    write_publish_result("started")
    client = GeminiClient(config, log)
    client.require_key()

    posts = load_posts(config)
    research = collect_research(config, log)
    if not research:
        log.log("publish_skipped", reason="no_research_items")
        write_publish_result("skipped", reason="no_research_items")
        return 0
    enrich_research_snippets(research, config, log)

    grounded_brief: dict[str, Any] | None = None
    if config.get("gemini", {}).get("enable_google_search_grounding", True):
        try:
            grounded_prompt = (
                "Find current high-interest, trustworthy technical article opportunities for Compile My Mind. "
                "Focus on software engineering, AI engineering, programming languages, systems design, developer tools, open source, databases, networking, cybersecurity, Microsoft cloud, certification, hardware, and cloud infrastructure. "
                "Return concise findings with citations."
            )
            grounded_brief = client.grounded_research(grounded_prompt)
            log.log("grounded_research_completed", citation_count=len(grounded_brief.get("citations", [])))
        except GeminiQuotaError as error:
            if grounded_research_fallback_enabled(config):
                grounded_brief = None
                log.log(
                    "grounded_research_fallback",
                    stage="grounded_research",
                    fallback="trusted_rss_feeds",
                    error=str(error),
                )
            else:
                log.log("publish_quota_limited", stage="grounded_research", error=str(error))
                state["last_runs"]["publish"] = {"time": iso_z(), "result": "quota_limited", "stage": "grounded_research"}
                save_state(state)
                write_publish_result("quota_limited", stage="grounded_research")
                return 0
        except GeminiTransientError as error:
            grounded_brief = None
            log.log(
                "grounded_research_transient_fallback",
                stage="grounded_research",
                fallback="trusted_rss_feeds",
                error=str(error),
            )
        except Exception as error:
            grounded_brief = None
            log.log("grounded_research_failed", error=str(error))

    try:
        topic = choose_topic(client, research, grounded_brief, posts, config, log)
    except GeminiQuotaError as error:
        log.log("publish_quota_limited", stage="topic_selection", error=str(error))
        state["last_runs"]["publish"] = {"time": iso_z(), "result": "quota_limited", "stage": "topic_selection"}
        save_state(state)
        write_publish_result("quota_limited", stage="topic_selection")
        return 0
    except GeminiTransientError as error:
        log.log("publish_retryable", stage="topic_selection", error=str(error))
        record_publish_retryable(state, "topic_selection", error)
        return 0
    if not topic:
        state["last_runs"]["publish"] = {"time": iso_z(), "result": "no_topic"}
        save_state(state)
        write_publish_result("no_topic")
        return 0

    feedback = ""
    attempts = int(config.get("publishing", {}).get("max_regeneration_attempts", 2)) + 1
    final_article: dict[str, Any] | None = None
    final_qa: dict[str, Any] | None = None
    for attempt in range(1, attempts + 1):
        log.log("article_generation_started", attempt=attempt, title=topic.get("title"))
        try:
            raw_article = client.generate_json(
                article_generation_prompt(topic, research, posts, config, feedback),
                task="article_generation",
            )
        except GeminiQuotaError as error:
            log.log("publish_quota_limited", stage="article_generation", attempt=attempt, error=str(error))
            state["last_runs"]["publish"] = {"time": iso_z(), "result": "quota_limited", "stage": "article_generation"}
            save_state(state)
            write_publish_result("quota_limited", stage="article_generation", attempt=attempt)
            return 0
        except GeminiTransientError as error:
            log.log("publish_retryable", stage="article_generation", attempt=attempt, error=str(error))
            record_publish_retryable(state, "article_generation", error)
            return 0
        article = normalize_article_payload(raw_article, topic, config, research)
        enrich_article_metadata(client, article, topic, config, log)
        issues = deterministic_qa(article, topic, posts, config)
        if issues:
            feedback = "\n".join(issues)
            log.log("article_deterministic_qa_failed", attempt=attempt, issues=issues)
            continue
        try:
            qa = ai_qa(client, article, topic, config, log)
        except GeminiQuotaError as error:
            log.log("publish_quota_limited", stage="quality_assurance", attempt=attempt, error=str(error))
            state["last_runs"]["publish"] = {"time": iso_z(), "result": "quota_limited", "stage": "quality_assurance"}
            save_state(state)
            write_publish_result("quota_limited", stage="quality_assurance", attempt=attempt)
            return 0
        except GeminiTransientError as error:
            log.log("publish_retryable", stage="quality_assurance", attempt=attempt, error=str(error))
            record_publish_retryable(state, "quality_assurance", error)
            return 0
        min_quality_score = float(config.get("publishing", {}).get("quality_min_score", 0.78))
        try:
            qa_score = float(qa.get("score", 0.0) or 0.0)
        except (TypeError, ValueError):
            qa_score = 0.0
        approved_value = qa.get("approved")
        approved = approved_value is True or str(approved_value).strip().lower() == "true"
        approved = approved and qa_score >= min_quality_score
        if approved:
            final_article = article
            final_qa = qa
            log.log("article_ai_qa_passed", score=qa.get("score"), reason=qa.get("reason", ""))
            break
        feedback = "\n".join(qa.get("required_fixes") or qa.get("issues") or ["AI QA rejected the article."])
        log.log("article_ai_qa_failed", attempt=attempt, score=qa.get("score"), feedback=feedback)

    if not final_article:
        state.setdefault("failures", []).append(
            {"time": iso_z(), "mode": "publish", "topic": topic.get("title"), "reason": "qa_failed", "feedback": feedback}
        )
        state["last_runs"]["publish"] = {"time": iso_z(), "result": "qa_failed"}
        save_state(state)
        log.log("publish_rejected_all_drafts", title=topic.get("title"), feedback=feedback)
        write_publish_result("rejected", reason="qa_failed", title=topic.get("title"))
        return 0

    try:
        index_path = write_article_bundle(final_article, topic, config, log, dry_run=args.dry_run)
    except ValueError as error:
        failed_post_dir = ROOT / config["site"].get("content_dir", "content/posts") / str(final_article.get("slug", ""))
        shutil.rmtree(failed_post_dir, ignore_errors=True)
        log.log("publish_rejected", reason="asset_validation_failed", error=str(error))
        write_publish_result("rejected", reason="asset_validation_failed", error=str(error))
        return 0
    if not args.dry_run and not run_hugo_build(log):
        if index_path.parent.exists():
            shutil.rmtree(index_path.parent)
        write_publish_result("rejected", reason="build_failed", path=str(index_path.relative_to(ROOT)))
        return 1

    state.setdefault("generated_posts", []).append(
        {
            "time": iso_z(),
            "title": final_article["title"],
            "slug": final_article["slug"],
            "categories": final_article["categories"],
            "tags": final_article["tags"],
            "sources": final_article.get("sources", []),
            "qa": final_qa,
            "path": str(index_path.relative_to(ROOT)),
        }
    )
    state["last_runs"]["publish"] = {"time": iso_z(), "result": "published" if not args.dry_run else "dry_run"}
    save_state(state)
    write_publish_result(
        "published" if not args.dry_run else "dry_run",
        path=str(index_path.relative_to(ROOT)),
        title=final_article["title"],
    )
    return 0


def extract_links(markdown: str) -> list[str]:
    # URLs inside code examples are not navigational article links and should
    # not be checked against the trusted-source allowlist.
    prose = re.sub(r"```.*?```|~~~.*?~~~", " ", markdown, flags=re.S)
    prose = re.sub(r"`[^`\n]+`", " ", prose)
    links = re.findall(r"!?\[[^\]]+\]\((https?://[^)\s]+)", prose)
    links.extend(re.findall(r"(?<!\()https?://[^\s)>\"]+", prose))
    cleaned: list[str] = []
    for link in links:
        link = link.rstrip(".,;:")
        if link not in cleaned:
            cleaned.append(link)
    return cleaned


def check_link(url: str, config: dict[str, Any]) -> tuple[bool, str]:
    timeout = int(config.get("maintenance", {}).get("link_timeout_seconds", 12))
    try:
        status, _body, _headers = http_request(url, method="HEAD", timeout=timeout, retries=1)
        if status in {405, 501}:
            status, _body, _headers = http_request(url, method="GET", timeout=timeout, retries=1)
        if status in {403, 429} and not config.get("maintenance", {}).get("treat_403_as_broken", False):
            return True, f"HTTP {status} (not treated as broken)"
        return status < 400, f"HTTP {status}"
    except Exception as error:
        return False, str(error)


def select_posts_for_maintenance(posts: list[Post], state: dict[str, Any], config: dict[str, Any], limit: int) -> list[Post]:
    reviews = state.get("maintenance_reviews", {})
    review_after_days = int(config.get("maintenance", {}).get("review_after_days", 45))
    now = utc_now()

    def priority(post: Post) -> tuple[int, float, str]:
        reviewed_raw = reviews.get(post.slug, {}).get("time")
        reviewed = parse_date(reviewed_raw)
        if reviewed:
            age = (now - reviewed.astimezone(dt.timezone.utc)).total_seconds() / 86400
        else:
            age = 9999
        stale_bonus = 0 if age >= review_after_days else 1
        post_date = parse_date(post.date) or dt.datetime(1970, 1, 1, tzinfo=dt.timezone.utc)
        return (stale_bonus, -age, post_date.isoformat())

    return sorted(posts, key=priority)[:limit]


def maintenance_prompt(post: Post, broken_links: list[dict[str, str]], grounded: dict[str, Any], config: dict[str, Any]) -> str:
    return f"""
You are maintaining an existing Compile My Mind article. Update only when meaningful.

Rules:
- If the article is still accurate and the broken link list is empty, return action "none".
- If facts are outdated, replace them with current supported facts from the grounded research.
- If links are broken, repair or remove them without changing the article's intent.
- Preserve the author's practical, educational tone.
- Do not add unsupported claims.
- Keep the Markdown body only; do not include YAML front matter.
- Preserve useful diagrams, charts, and image references unless they are wrong.

Article metadata:
{json.dumps({k: post.frontmatter.get(k) for k in ["title", "description", "tags", "categories", "date"]}, ensure_ascii=False, indent=2)}

Current Markdown:
{post.body[:24000]}

Broken links:
{json.dumps(broken_links, ensure_ascii=False, indent=2)}

Grounded research:
{json.dumps(grounded, ensure_ascii=False, indent=2)}

Return JSON only:
{{
  "action": "none",
  "reason": "why no update is needed or what changed",
  "updated_markdown": "",
  "description": "",
  "tags": [],
  "categories": [],
  "sources": []
}}
""".strip()


def update_post_file(post: Post, payload: dict[str, Any], config: dict[str, Any]) -> None:
    frontmatter = dict(post.frontmatter)
    body = remove_accidental_frontmatter(str(payload.get("updated_markdown", "")).strip())
    sources = payload.get("sources", []) or []
    if sources:
        body = ensure_sources_section(body, sources)
    if payload.get("description"):
        frontmatter["description"] = normalize_space(str(payload["description"]))[:180]
    if payload.get("summary"):
        frontmatter["summary"] = normalize_space(str(payload["summary"]))[:320]
    if payload.get("tags"):
        frontmatter["tags"] = sanitize_tags(payload["tags"], {"title": post.title})
    if payload.get("categories"):
        frontmatter["categories"] = sanitize_categories(payload["categories"], None, config)
    frontmatter["lastmod"] = local_now(config).replace(microsecond=0).isoformat()
    frontmatter["last_reviewed"] = local_now(config).date().isoformat()
    post.path.write_text(compose_markdown(frontmatter, body), encoding="utf-8")


def run_maintain(args: argparse.Namespace) -> int:
    config = load_config()
    state = load_state()
    log = EventLog()
    client = GeminiClient(config, log)
    client.require_key()
    posts = load_posts(config)
    limit = args.max_articles or int(config.get("maintenance", {}).get("max_articles_per_run", 1))
    selected = select_posts_for_maintenance(posts, state, config, limit)
    log.log("maintenance_selected", count=len(selected), slugs=[post.slug for post in selected])
    exit_code = 0
    quota_limited = False
    for post in selected:
        links = extract_links(post.body)
        broken: list[dict[str, str]] = []
        for link in links:
            ok, detail = check_link(link, config)
            if not ok:
                broken.append({"url": link, "status": detail})
        grounded: dict[str, Any] = {"text": "", "citations": []}
        try:
            grounded = client.grounded_research(
                f"Find current, trustworthy updates relevant to this technical article: {post.title}. "
                f"Focus on facts that may have changed since {post.date}. Include citations."
            )
        except GeminiQuotaError as error:
            log.log("maintenance_quota_limited", slug=post.slug, stage="grounded_research", error=str(error))
            quota_limited = True
            break
        except Exception as error:
            log.log("maintenance_grounded_research_failed", slug=post.slug, error=str(error))
            continue
        try:
            payload = client.generate_json(
                maintenance_prompt(post, broken, grounded, config),
                model=client.text_model,
                task="maintenance_review",
            )
        except GeminiQuotaError as error:
            log.log("maintenance_quota_limited", slug=post.slug, stage="maintenance_review", error=str(error))
            quota_limited = True
            break
        except Exception as error:
            log.log("maintenance_ai_failed", slug=post.slug, error=str(error))
            exit_code = 1
            continue
        action = str(payload.get("action", "none")).lower()
        if action != "update":
            state.setdefault("maintenance_reviews", {})[post.slug] = {
                "time": iso_z(),
                "action": "none",
                "reason": payload.get("reason", ""),
                "broken_links": broken,
            }
            log.log("maintenance_no_update", slug=post.slug, reason=payload.get("reason", ""), broken_count=len(broken))
            continue
        updated = str(payload.get("updated_markdown", ""))
        grounded_urls = {
            str(source.get("url", "")).strip()
            for source in grounded.get("citations", []) or []
            if isinstance(source, dict) and source.get("url")
        }
        existing_urls = set(extract_links(post.body))
        allowed_urls = existing_urls | grounded_urls
        site_base = str(config.get("site", {}).get("base_url", "")).rstrip("/")
        untrusted_urls = [
            url
            for url in extract_links(updated)
            if url not in allowed_urls and not (site_base and url.startswith(site_base))
        ]
        if untrusted_urls:
            log.log("maintenance_update_rejected", slug=post.slug, reason="untrusted_new_links", urls=untrusted_urls[:10])
            exit_code = 1
            continue
        payload["sources"] = [
            source
            for source in (payload.get("sources", []) or [])
            if isinstance(source, dict) and str(source.get("url", "")).strip() in allowed_urls
        ]
        if word_count(updated) < max(500, int(word_count(post.body) * 0.55)):
            log.log("maintenance_update_rejected", slug=post.slug, reason="updated_body_too_short")
            exit_code = 1
            continue
        if cosine_similarity(post.body, updated) < float(config.get("maintenance", {}).get("update_similarity_floor", 0.55)):
            log.log("maintenance_update_rejected", slug=post.slug, reason="update_changed_article_too_much")
            exit_code = 1
            continue
        if args.dry_run:
            log.log("maintenance_dry_run_update_ready", slug=post.slug, reason=payload.get("reason", ""))
        else:
            update_post_file(post, payload, config)
            log.log("maintenance_post_updated", slug=post.slug, reason=payload.get("reason", ""))
        state.setdefault("maintenance_reviews", {})[post.slug] = {
            "time": iso_z(),
            "action": "update" if not args.dry_run else "dry_run",
            "reason": payload.get("reason", ""),
            "broken_links": broken,
        }
    result = "quota_limited" if quota_limited else ("completed_with_errors" if exit_code else "completed")
    state["last_runs"]["maintain"] = {"time": iso_z(), "result": result}
    save_state(state)
    if not args.dry_run and exit_code == 0 and selected:
        if not run_hugo_build(log):
            return 1
    return exit_code


def run_audit(_args: argparse.Namespace) -> int:
    config = load_config()
    posts = load_posts(config)
    log = EventLog()
    counts = category_counts(posts, config)
    log.log(
        "audit",
        posts=len(posts),
        category_counts=counts,
        target_category=target_category(posts, config),
        hugo_available=bool(shutil.which("hugo")),
    )
    duplicate_slugs = [slug for slug, count in Counter(post.slug for post in posts).items() if count > 1]
    asset_issues = content_asset_issues(config)
    if duplicate_slugs or asset_issues:
        log.log("audit_failed", duplicate_slugs=duplicate_slugs, asset_issues=asset_issues[:50], issue_count=len(asset_issues))
        return 1
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Autonomous publishing engine for Compile My Mind.")
    parser.add_argument("--mode", choices=["publish", "maintain", "audit"], required=True)
    parser.add_argument("--dry-run", action="store_true", help="Run decisions without writing article updates.")
    parser.add_argument("--max-articles", type=int, default=None, help="Maximum articles to review in maintenance mode.")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.mode == "publish":
        return run_publish(args)
    if args.mode == "maintain":
        return run_maintain(args)
    if args.mode == "audit":
        return run_audit(args)
    parser.error(f"Unknown mode: {args.mode}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
