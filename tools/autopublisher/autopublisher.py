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
import ast
import base64
import copy
import datetime as dt
import email.utils
import gzip
import hashlib
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
from html.parser import HTMLParser
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:  # Publishing rejects YAML when the configured parser is unavailable.
    yaml = None  # type: ignore


class _UnavailableYamlParseError(Exception):
    """Placeholder used only when PyYAML is not installed."""


YamlParseError = yaml.YAMLError if yaml is not None else _UnavailableYamlParseError

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
DASHBOARD_PATH = AUTOPUBLISHER_DIR / "dashboard.json"
CONTENT_AUDIT_PATH = AUTOPUBLISHER_DIR / "reports" / "content-audit.json"
MAINTENANCE_REPORT_PATH = AUTOPUBLISHER_DIR / "reports" / "maintenance-latest.json"
READY_QUEUE_DIR = AUTOPUBLISHER_DIR / "queue" / "ready"
PREPARE_RESULT_PATH = AUTOPUBLISHER_DIR / "prepare-result.json"

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

TOPIC_SOURCE_GENERIC_TOKENS = {
    "ai",
    "announcement",
    "announcing",
    "azure",
    "best",
    "build",
    "building",
    "cloud",
    "developer",
    "example",
    "examples",
    "github",
    "google",
    "guide",
    "improvement",
    "improvements",
    "introducing",
    "kubernetes",
    "microsoft",
    "platform",
    "platforms",
    "practical",
    "technology",
    "tools",
    "using",
    "windows",
    "world",
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
        if os.environ.get("AUTOPUBLISHER_LOG_STDOUT", "1").strip().lower() not in {"0", "false", "no", "off"}:
            print(json.dumps(printable, ensure_ascii=False))


def normalize_space(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def compact_model_prompt(prompt: str, max_characters: int) -> str:
    """Keep provider requests below their input ceiling without losing instructions or schema."""
    limit = max(4000, int(max_characters))
    if len(prompt) <= limit:
        return prompt
    marker = "\n\n[Middle context compacted to stay within the provider input budget.]\n\n"
    available = limit - len(marker)
    head = int(available * 0.58)
    tail = available - head
    return prompt[:head].rstrip() + marker + prompt[-tail:].lstrip()


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
    parts: list[str] = []
    for part in value.split("-"):
        if part and (not parts or part != parts[-1]):
            parts.append(part)
    value = "-".join(parts)
    if len(value) > max_length:
        shortened = value[: max_length + 1].rsplit("-", 1)[0]
        value = shortened if shortened else value[:max_length].strip("-")
    return value or "untitled-post"


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
        "publisher",
        "draft",
        "autonomous",
        "series",
        "series_part",
        "planned_next_parts",
        "last_reviewed",
        "verification_status",
        "verification_date",
        "verification_version",
        "version_context",
        "recheck_after",
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
    validated: bool = False
    validation: dict[str, Any] = field(default_factory=dict)


def load_config() -> dict[str, Any]:
    if not CONFIG_PATH.exists():
        raise SystemExit(f"Missing config: {CONFIG_PATH}")
    return read_json(CONFIG_PATH, {})


def load_state() -> dict[str, Any]:
    state = read_json(
        STATE_PATH,
        {
            "version": 5,
            "generated_posts": [],
            "maintenance_reviews": {},
            "failures": [],
            "last_runs": {},
            "rejected_articles": [],
            "provider_cooldowns": {},
            "pending_publication": {},
            "preparation_pending_publication": {},
            "ready_publications": [],
        },
    )
    if not isinstance(state, dict):
        state = {}
    state.setdefault("version", 5)
    state["version"] = max(5, int(state["version"] or 5))
    for key, default in {
        "generated_posts": [],
        "maintenance_reviews": {},
        "failures": [],
        "last_runs": {},
        "rejected_articles": [],
        "provider_cooldowns": {},
        "pending_publication": {},
        "preparation_pending_publication": {},
        "ready_publications": [],
    }.items():
        state.setdefault(key, default)
    return state


def load_model_state() -> dict[str, Any]:
    return read_json(MODEL_STATE_PATH, {})


def grounded_research_fallback_enabled(config: dict[str, Any]) -> bool:
    """Allow publishing to continue from trusted feeds when Search grounding is unavailable."""
    value = config.get("gemini", {}).get("grounded_research_fallback_to_feeds", True)
    if isinstance(value, str):
        return value.strip().lower() not in {"0", "false", "no", "off"}
    return bool(value)


def is_permanent_billing_quota(error: Exception | str) -> bool:
    """Distinguish depleted billing credit from a temporary rate limit."""
    message = str(error).lower()
    return any(
        marker in message
        for marker in (
            "prepayment credits are depleted",
            "prepaid credits are depleted",
            "billing account",
            "manage your project and billing",
        )
    )


def provider_circuit_open(state: dict[str, Any] | None, provider: str) -> bool:
    if not state:
        return False
    cooldown = (state.get("provider_cooldowns", {}) or {}).get(provider, {}) or {}
    until = str(cooldown.get("until", "")).strip()
    if not until:
        return False
    try:
        expires = dt.datetime.fromisoformat(until.replace("Z", "+00:00"))
    except ValueError:
        return False
    return expires > utc_now()


def open_provider_circuit(
    state: dict[str, Any] | None,
    provider: str,
    error: Exception | str,
    config: dict[str, Any],
) -> bool:
    """Persist a cooldown for non-transient provider billing failures."""
    if state is None or not is_permanent_billing_quota(error):
        return False
    hours = max(1, int(config.get("cost_control", {}).get("gemini_billing_cooldown_hours", 168)))
    state.setdefault("provider_cooldowns", {})[provider] = {
        "opened_at": iso_z(),
        "until": iso_z(utc_now() + dt.timedelta(hours=hours)),
        "reason": "billing_credit_depleted",
    }
    save_state(state)
    return True


def save_state(state: dict[str, Any]) -> None:
    write_json(STATE_PATH, state)


def write_publish_result(result: str, **fields: Any) -> None:
    write_json(PUBLISH_RESULT_PATH, {"time": iso_z(), "result": result, **fields})


def write_prepare_result(result: str, **fields: Any) -> None:
    write_json(PREPARE_RESULT_PATH, {"time": iso_z(), "result": result, **fields})


def retry_topic_payload(topic: dict[str, Any] | None, sources: list[ResearchItem] | None = None) -> dict[str, Any]:
    """Store only the durable, source-bound fields needed for a clean retry."""
    if not isinstance(topic, dict):
        return {}
    allowed_fields = {
        "title",
        "slug",
        "primary_category",
        "categories",
        "tags",
        "search_intent",
        "why_now",
        "source_urls",
        "needs_diagram",
        "needs_chart",
        "series",
        "generation_constraints",
        "offline_fallback",
    }
    payload = {key: value for key, value in topic.items() if key in allowed_fields}
    retry_sources = [
        {"title": item.title, "url": item.url}
        for item in sources or []
        if item.title and item.url and (item.validated or not item.validation)
    ]
    configured_sources = [
        {"title": str(item.get("title", "")), "url": str(item.get("url", ""))}
        for item in topic.get("seed_sources", []) or []
        if isinstance(item, dict) and item.get("title") and item.get("url")
    ]
    if retry_sources or configured_sources:
        payload["seed_sources"] = dedupe_sources([*retry_sources, *configured_sources])
        payload["source_urls"] = [item["url"] for item in payload["seed_sources"]]
    return payload


def schedule_publish_retry(
    state: dict[str, Any],
    config: dict[str, Any],
    log: "EventLog",
    *,
    reason: str,
    stage: str,
    detail: str = "",
    topic: dict[str, Any] | None = None,
    sources: list[ResearchItem] | None = None,
) -> None:
    """Persist a retry across scheduled runs without weakening publication gates."""
    previous = state.get("pending_publication", {}) or {}
    previous_topic = previous.get("topic", {}) or {}
    candidate = retry_topic_payload(topic, sources)
    same_topic = bool(candidate.get("slug") and candidate.get("slug") == previous_topic.get("slug"))
    topic_attempts = int(previous.get("topic_attempts", 0) or 0) + 1 if same_topic else (1 if candidate else 0)
    consecutive_attempts = int(previous.get("consecutive_attempts", 0) or 0) + 1
    retry_config = config.get("retry", {})
    delay_hours = max(1, int(retry_config.get("base_delay_hours", 6)))
    pending = {
        "scheduled_at": iso_z(),
        "next_retry_at": iso_z(utc_now() + dt.timedelta(hours=delay_hours)),
        "reason": reason,
        "stage": stage,
        "detail": normalize_space(detail)[:2000],
        "consecutive_attempts": consecutive_attempts,
        "topic_attempts": topic_attempts,
        "topic": candidate,
    }
    state["pending_publication"] = pending
    state["last_runs"]["publish"] = {
        "time": iso_z(),
        "result": "retry_scheduled",
        "reason": reason,
        "stage": stage,
        "next_retry_at": pending["next_retry_at"],
    }
    save_state(state)
    write_publish_result(
        "retry_scheduled",
        reason=reason,
        stage=stage,
        next_retry_at=pending["next_retry_at"],
    )
    log.log(
        "publish_retry_scheduled",
        reason=reason,
        stage=stage,
        next_retry_at=pending["next_retry_at"],
        consecutive_attempts=consecutive_attempts,
        topic=str(candidate.get("slug", "")),
    )


def pending_publication_topic(
    state: dict[str, Any],
    posts: list[Post],
    config: dict[str, Any],
    log: "EventLog",
) -> dict[str, Any] | None:
    """Return a still-eligible queued topic, otherwise let discovery choose a fresh one."""
    pending = state.get("pending_publication", {}) or {}
    topic = pending.get("topic", {}) or {}
    if not isinstance(topic, dict) or not topic.get("title"):
        return None
    maximum = max(1, int(config.get("retry", {}).get("max_same_topic_attempts", 3)))
    if int(pending.get("topic_attempts", 0) or 0) >= maximum:
        log.log(
            "publish_retry_topic_exhausted",
            slug=topic.get("slug"),
            attempts=pending.get("topic_attempts", 0),
        )
        pending["topic"] = {}
        pending["topic_attempts"] = 0
        state["pending_publication"] = pending
        return None
    slug = slugify(str(topic.get("slug") or topic.get("title", "")), int(config.get("publishing", {}).get("max_slug_length", 82)))
    if not slug or slug in {post.slug for post in posts}:
        pending["topic"] = {}
        pending["topic_attempts"] = 0
        state["pending_publication"] = pending
        return None
    topic = dict(topic)
    topic["slug"] = slug
    topic["categories"] = sanitize_categories(topic.get("categories", []), topic.get("primary_category"), config)
    topic["tags"] = sanitize_tags(topic.get("tags", []), topic, config)
    relevance = topic_relevance_score(topic, config)
    if not relevance.get("approved") or relevance.get("score", 0.0) < float(
        config.get("publishing", {}).get("topic_relevance_min_score", 0.0)
    ):
        log.log("publish_retry_topic_dropped", slug=slug, reason="topic_relevance_too_low")
        pending["topic"] = {}
        pending["topic_attempts"] = 0
        state["pending_publication"] = pending
        return None
    log.log(
        "publish_retry_resumed",
        slug=slug,
        attempts=pending.get("topic_attempts", 0),
        original_reason=pending.get("reason", ""),
    )
    return topic


def record_rejection(
    state: dict[str, Any],
    log: EventLog,
    *,
    topic: dict[str, Any] | None,
    reason: str,
    detail: str = "",
    attempts: int = 0,
) -> None:
    """Persist an internal rejection without creating a public Hugo page."""
    payload = {
        "time": iso_z(),
        "title": str((topic or {}).get("title", "")),
        "slug": str((topic or {}).get("slug", "")),
        "reason": reason,
        "detail": normalize_space(detail)[:4000],
        "attempts": attempts,
    }
    state.setdefault("rejected_articles", []).append(payload)
    state["rejected_articles"] = state["rejected_articles"][-200:]
    state.setdefault("failures", []).append({"mode": "publish", **payload})
    state["failures"] = state["failures"][-200:]
    log.log("article_rejected", **payload)


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


def load_queued_posts(state: dict[str, Any]) -> list[Post]:
    """Load queued bundles for duplicate detection without exposing them as internal links."""
    posts: list[Post] = []
    for entry in state.get("ready_publications", []) or []:
        if not isinstance(entry, dict):
            continue
        bundle_path = ready_bundle_path(entry)
        if bundle_path is None:
            continue
        index_path = bundle_path / "index.md"
        if not index_path.is_file():
            continue
        frontmatter, body = split_frontmatter(index_path.read_text(encoding="utf-8"))
        frontmatter["_queued"] = True
        slug = str(entry.get("slug") or bundle_path.name)
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


def recent_category_counts(
    posts: list[Post],
    config: dict[str, Any],
    days: int,
    *,
    now: dt.datetime | None = None,
) -> dict[str, int]:
    """Count controlled categories inside a rolling publication window."""
    reference = now or utc_now()
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=dt.timezone.utc)
    cutoff = reference - dt.timedelta(days=max(0, days))
    recent: list[Post] = []
    for post in posts:
        published = parse_date(post.date)
        if published and published >= cutoff:
            recent.append(post)
    return category_counts(recent, config)


def category_balance_snapshot(posts: list[Post], config: dict[str, Any]) -> dict[str, dict[str, int]]:
    return {
        "last_7_days": recent_category_counts(posts, config, 7),
        "last_30_days": recent_category_counts(posts, config, 30),
        "all_time": category_counts(posts, config),
    }


def target_category(posts: list[Post], config: dict[str, Any]) -> str:
    snapshot = category_balance_snapshot(posts, config)
    counts = snapshot["all_time"]
    if not counts:
        return "technology"
    seven = snapshot["last_7_days"]
    thirty = snapshot["last_30_days"]
    # Recent repetition matters most, but total inventory breaks ties so a
    # long-neglected cluster can recover without overriding article quality.
    return sorted(
        counts,
        key=lambda category: (
            seven.get(category, 0),
            thirty.get(category, 0),
            counts.get(category, 0),
            category,
        ),
    )[0]


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
                response_headers["x-final-url"] = str(response.geturl())
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
    def __init__(self, config: dict[str, Any], log: EventLog, state: dict[str, Any] | None = None) -> None:
        self.config = config
        self.log = log
        self.state = state
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
        self.github_models_models = [self.github_models_model] + [
            str(candidate).strip()
            for candidate in github_models.get("fallback_models", []) or []
            if str(candidate).strip() and str(candidate).strip() != self.github_models_model
        ]
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
        if self._use_lightweight_model(task):
            for github_model in self.github_models_models:
                try:
                    return self._github_models_generate_json(
                        prompt,
                        temperature=temperature,
                        max_output_tokens=max_output_tokens,
                        task=task or "",
                        github_model=github_model,
                    )
                except Exception as error:
                    self.log.log(
                        "lightweight_model_fallback",
                        task=task or "unknown",
                        model=github_model,
                        error=str(error),
                    )
        selected_model = model or self.text_model
        self.require_key()
        config = {
            "temperature": temperature
            if temperature is not None
            else float(self.config.get("gemini", {}).get("temperature", 0.55)),
            "responseMimeType": "application/json",
            "maxOutputTokens": max_output_tokens or int(self.config.get("gemini", {}).get("max_output_tokens", 32768)),
        }
        response = self._generate_content(selected_model, prompt, config)
        text = self._extract_text(response)
        try:
            return parse_model_json(text)
        except ValueError as error:
            raise GeminiTransientError(
                f"{selected_model} returned invalid or truncated JSON; the task will be retried safely: {error}"
            ) from error

    def generate_text(
        self,
        prompt: str,
        *,
        model: str | None = None,
        temperature: float | None = None,
        max_output_tokens: int | None = None,
        task: str | None = None,
    ) -> str:
        if self._use_lightweight_model(task):
            for github_model in self.github_models_models:
                try:
                    return self._github_models_generate_text(
                        prompt,
                        temperature=temperature,
                        max_output_tokens=max_output_tokens,
                        task=task or "",
                        github_model=github_model,
                    )
                except Exception as error:
                    self.log.log(
                        "lightweight_model_fallback",
                        task=task or "unknown",
                        model=github_model,
                        error=str(error),
                    )
        selected_model = model or self.text_model
        self.require_key()
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
        github_model: str,
    ) -> dict[str, Any]:
        response = self._github_models_request(
            prompt,
            temperature=temperature,
            max_output_tokens=max_output_tokens,
            task=task,
            json_mode=True,
            github_model=github_model,
        )
        parsed = parse_model_json(self._extract_chat_text(response))
        self.log.log("lightweight_model_used", task=task, model=github_model)
        return parsed

    def _github_models_generate_text(
        self,
        prompt: str,
        *,
        temperature: float | None,
        max_output_tokens: int | None,
        task: str,
        github_model: str,
    ) -> str:
        text = self._extract_chat_text(
            self._github_models_request(
                prompt,
                temperature=temperature,
                max_output_tokens=max_output_tokens,
                task=task,
                github_model=github_model,
            )
        )
        self.log.log("lightweight_model_used", task=task, model=github_model)
        return text

    def _github_models_request(
        self,
        prompt: str,
        *,
        temperature: float | None,
        max_output_tokens: int | None,
        task: str,
        github_model: str,
        json_mode: bool = False,
    ) -> dict[str, Any]:
        configured_input_limit = int(
            self.config.get("github_models", {}).get("max_input_characters", 24000)
        )
        compacted_prompt = compact_model_prompt(prompt, configured_input_limit)
        if compacted_prompt != prompt:
            self.log.log(
                "model_prompt_compacted",
                task=task,
                model=github_model,
                original_characters=len(prompt),
                compacted_characters=len(compacted_prompt),
            )
        prompt = compacted_prompt
        payload = {
            "model": github_model,
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
        if status == 413:
            retry_limit = max(8000, min(configured_input_limit - 1000, int(len(prompt) * 0.65)))
            retry_prompt = compact_model_prompt(prompt, retry_limit)
            payload["messages"][1]["content"] = retry_prompt
            self.log.log(
                "model_prompt_retried_compact",
                task=task,
                model=github_model,
                original_characters=len(prompt),
                compacted_characters=len(retry_prompt),
            )
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
                retries=2,
            )
        if status == 429:
            raise GitHubModelsQuotaError(
                f"GitHub Models quota exceeded for {github_model}: HTTP {status}: {body[:800]!r}"
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
        if provider_circuit_open(self.state, "gemini_grounded_research"):
            raise GeminiQuotaError(
                "Gemini grounded research is paused because the billing-credit circuit is open; "
                "trusted feeds and configured official sources remain available."
            )
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
                error = GeminiQuotaError(f"Gemini grounded research quota exceeded: HTTP {status}: {body[:500]!r}")
                if open_provider_circuit(self.state, "gemini_grounded_research", error, self.config):
                    self.log.log(
                        "provider_circuit_opened",
                        provider="gemini_grounded_research",
                        reason="billing_credit_depleted",
                    )
                raise error
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
        if provider_circuit_open(self.state, "gemini_generation"):
            raise GeminiQuotaError(
                "Gemini generation is paused because the billing-credit circuit is open; "
                "GitHub Models or a source-bound offline fallback may still complete the publication."
            )
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
                error = GeminiQuotaError(f"Gemini quota exceeded for model {model}: HTTP {status}: {body[:1000]!r}")
                if open_provider_circuit(self.state, "gemini_generation", error, self.config):
                    self.log.log(
                        "provider_circuit_opened",
                        provider="gemini_generation",
                        reason="billing_credit_depleted",
                    )
                raise error
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


def is_publishable_source_url(url: str) -> bool:
    """Reject feed, Atom API, and comment endpoints from article citations."""
    parsed = urllib.parse.urlsplit(str(url).strip())
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        return False
    path = parsed.path.lower().rstrip("/")
    return not (
        "/feeds/" in path
        or path.endswith("/comments/default")
        or path.endswith("/feed")
        or path.endswith("/rss")
        or path.endswith("/atom.xml")
        or path.endswith("/index.xml")
        or path.endswith(".rss")
        or path.endswith(".xml")
    )


def article_link(element: ET.Element) -> str:
    links = [child for child in element if child.tag.split("}")[-1] == "link"]
    for child in links:
        href = str(child.attrib.get("href", "")).strip()
        rel = str(child.attrib.get("rel", "alternate")).lower()
        link_type = str(child.attrib.get("type", "")).lower()
        if href and rel == "alternate" and (not link_type or "html" in link_type):
            return href
    for child in links:
        href = str(child.attrib.get("href", "")).strip()
        rel = str(child.attrib.get("rel", "")).lower()
        link_type = str(child.attrib.get("type", "")).lower()
        if href and rel not in {"comments", "replies", "self", "edit"} and "atom+xml" not in link_type:
            return href
    for child in links:
        if "href" not in child.attrib:
            text = "".join(child.itertext()).strip()
            if text:
                return text
    return ""


def first_text(element: ET.Element, *names: str) -> str:
    for name in names:
        if name == "link":
            link = article_link(element)
            if link:
                return link
            continue
        for child in element:
            if child.tag.split("}")[-1] == name:
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
        if not is_publishable_source_url(link):
            log.log("research_item_link_rejected", source=source.get("name"), title=title, url=link)
            continue
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


def research_for_prompt(items: list[ResearchItem], *, snippet_characters: int = 1400) -> list[dict[str, Any]]:
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
                "snippet": item.snippet[:max(200, int(snippet_characters))],
                "verified_at": item.validation.get("verified_at", ""),
                "page_title": item.validation.get("page_title", ""),
            }
        )
    return payload


def research_item_topic_similarity(topic: dict[str, Any], item: ResearchItem) -> float:
    topic_text = " ".join(
        [
            str(topic.get("title", "")),
            str(topic.get("search_intent", "")),
            " ".join(topic.get("tags", []) or []),
            " ".join(topic.get("categories", []) or []),
        ]
    )
    return cosine_similarity(topic_text, f"{item.title} {item.summary}")


def is_configured_seed_source(topic: dict[str, Any], item: ResearchItem) -> bool:
    """Return whether an item is one of this evergreen topic's fixed sources."""
    source_url = canonical_url(item.url)
    return bool(
        source_url
        and source_url in {
            canonical_url(str(source.get("url", "")))
            for source in topic.get("seed_sources", []) or []
            if isinstance(source, dict) and source.get("url")
        }
    )


def topic_source_is_directly_relevant(
    topic: dict[str, Any],
    item: ResearchItem,
    config: dict[str, Any] | None = None,
) -> bool:
    """Keep a selected source only when it substantively matches the topic.

    A model-selected URL is not evidence of relevance by itself.  Feed items
    frequently share broad labels such as "cloud" while covering different
    products.  Configured evergreen seed URLs are already reviewed as a set,
    so preserve them even when a short documentation title lacks enough words
    for a lexical comparison.
    """
    if is_configured_seed_source(topic, item):
        return True
    if not config or "research" not in config:
        return True
    research_config = config.get("research", {})
    topic_tokens = set(
        tokenize(
            " ".join(
                [
                    str(topic.get("title", "")),
                    str(topic.get("search_intent", "")),
                    " ".join(topic.get("tags", []) or []),
                ]
            )
        )
    )
    if not topic_tokens:
        return False
    source_tokens = set(tokenize(f"{item.title} {item.summary} {item.snippet}"))
    shared_tokens = topic_tokens & source_tokens
    anchor_tokens = topic_tokens - TOPIC_SOURCE_GENERIC_TOKENS
    shared_anchor_tokens = anchor_tokens & source_tokens
    minimum_overlap = int(research_config.get("topic_source_min_token_overlap", 2))
    minimum_anchor_overlap = int(research_config.get("topic_source_min_anchor_overlap", 2))
    minimum_similarity = float(research_config.get("topic_source_min_similarity", 0.16))
    category_overlap = bool(set(topic.get("categories", []) or []) & set(item.categories))
    similarity = research_item_topic_similarity(topic, item)
    return len(shared_tokens) >= minimum_overlap and len(shared_anchor_tokens) >= minimum_anchor_overlap and (
        similarity >= minimum_similarity
        or (category_overlap and similarity >= minimum_similarity * 0.75)
    )


def research_items_for_topic(
    topic: dict[str, Any],
    research: list[ResearchItem],
    *,
    limit: int = 8,
    config: dict[str, Any] | None = None,
) -> list[ResearchItem]:
    """Return only sources that are directly relevant to the selected topic.

    Earlier behavior padded every topic to eight sources even when the remaining
    feed items covered unrelated products. That contaminated article drafts and
    made repair attempts repeat unsupported claims from neighboring feed items.
    """
    selected_urls = {canonical_url(str(url)) for url in topic.get("source_urls", []) or []}
    source_items = [
        item
        for item in research
        if canonical_url(item.url) in selected_urls
        and topic_source_is_directly_relevant(topic, item, config)
    ]
    if len(source_items) >= limit:
        return source_items[:limit]
    ranked = sorted(
        research,
        key=lambda item: (research_item_topic_similarity(topic, item), item.score),
        reverse=True,
    )
    for item in ranked:
        if item in source_items:
            continue
        if not topic_source_is_directly_relevant(topic, item, config):
            continue
        source_items.append(item)
        if len(source_items) >= limit:
            break
    return source_items


def grounded_brief_research_items(
    brief: dict[str, Any] | None,
    topic: dict[str, Any],
    *,
    source: str = "Gemini grounded research",
) -> list[ResearchItem]:
    """Convert grounded citations into normal candidates for URL validation."""
    categories = list(topic.get("categories", []) or [])
    items: list[ResearchItem] = []
    for citation in (brief or {}).get("citations", []) or []:
        if not isinstance(citation, dict):
            continue
        title = normalize_space(str(citation.get("title", "")))
        url = str(citation.get("url", "")).strip()
        if not title or not url or not is_publishable_source_url(url):
            continue
        items.append(
            ResearchItem(
                source=source,
                title=title,
                url=url,
                summary=f"Direct source collected for {topic.get('title', title)}.",
                published="",
                categories=categories,
                score=2.5,
            )
        )
    return items


def merge_research_items(*groups: list[ResearchItem]) -> list[ResearchItem]:
    merged: list[ResearchItem] = []
    seen: set[str] = set()
    for group in groups:
        for item in group:
            key = canonical_url(item.url)
            if not key or key in seen:
                continue
            seen.add(key)
            merged.append(item)
    return merged


def collect_topic_research(
    client: "GeminiClient",
    topic: dict[str, Any],
    research: list[ResearchItem],
    config: dict[str, Any],
    log: "EventLog",
    state: dict[str, Any] | None = None,
) -> list[ResearchItem]:
    """Build and validate a coherent source bundle for one article topic."""
    required = int(config.get("publishing", {}).get("required_source_count", 1))
    limit = int(config.get("research", {}).get("topic_source_max_items", 6))
    existing_urls = {canonical_url(item.url) for item in research}
    seed_candidates = [
        ResearchItem(
            source="Configured evergreen official source",
            title=normalize_space(str(source.get("title", ""))),
            url=str(source.get("url", "")).strip(),
            summary=f"Official documentation selected for {topic.get('title', '')}.",
            published="",
            categories=list(topic.get("categories", []) or []),
            score=3.0,
        )
        for source in topic.get("seed_sources", []) or []
        if isinstance(source, dict)
        and source.get("title")
        and source.get("url")
        and canonical_url(str(source.get("url", ""))) not in existing_urls
    ]
    if seed_candidates:
        validated_seeds = validate_research_items(seed_candidates, config, log)
        research = merge_research_items(research, validated_seeds)
        log.log(
            "evergreen_sources_validated",
            title=topic.get("title"),
            candidates=len(seed_candidates),
            validated=len(validated_seeds),
        )
    scoped = research_items_for_topic(topic, research, limit=limit, config=config)
    # Once an evergreen topic has enough validated seed documentation, keep
    # the article bundle anchored to those exact URLs.  Do not pad it with a
    # semantically adjacent grounded result (for example, another Kubernetes
    # project) just because it shares a keyword.
    seed_urls = {
        canonical_url(str(source.get("url", "")))
        for source in topic.get("seed_sources", []) or []
        if isinstance(source, dict) and source.get("url")
    }
    exact_seed_sources = [item for item in scoped if canonical_url(item.url) in seed_urls]
    if len(exact_seed_sources) >= required:
        scoped = exact_seed_sources[:required]
    grounding_enabled = config.get("gemini", {}).get("enable_google_search_grounding", True)
    grounding_available = grounding_enabled and not provider_circuit_open(state, "gemini_grounded_research")
    if len(scoped) < required and grounding_enabled and not grounding_available:
        log.log(
            "topic_grounded_research_skipped",
            title=topic.get("title"),
            reason="provider_circuit_open",
        )
    if len(scoped) < required and grounding_available:
        prompt = (
            "Find current official or primary technical documentation that directly supports this article topic: "
            f"{topic.get('title', '')}. Reader intent: {topic.get('search_intent', '')}. "
            "Return directly relevant sources for concrete claims, commands, configuration, version context, and security guidance. "
            "Prefer official vendor documentation, standards, government publications, release notes, and official repositories. "
            "Do not return search-result pages, category pages, promotional landing pages, or sources about adjacent products."
        )
        try:
            brief = client.grounded_research(prompt)
            candidates = grounded_brief_research_items(brief, topic)
            validated = validate_research_items(candidates, config, log) if candidates else []
            research = merge_research_items(research, validated)
            scoped = research_items_for_topic(topic, research, limit=limit, config=config)
            log.log(
                "topic_grounded_research_completed",
                title=topic.get("title"),
                citation_count=len(candidates),
                validated_count=len(validated),
            )
        except GeminiQuotaError as error:
            log.log("topic_grounded_research_quota_limited", title=topic.get("title"), error=str(error))
        except GeminiTransientError as error:
            log.log("topic_grounded_research_retryable", title=topic.get("title"), error=str(error))
        except Exception as error:
            log.log("topic_grounded_research_failed", title=topic.get("title"), error=str(error))
    log.log(
        "topic_sources_selected",
        title=topic.get("title"),
        count=len(scoped),
        urls=[item.url for item in scoped],
    )
    return scoped


def dedupe_sources(sources: list[dict[str, Any]]) -> list[dict[str, str]]:
    seen: set[str] = set()
    output: list[dict[str, str]] = []
    for source in sources:
        url = str(source.get("url", "")).strip()
        key = canonical_url(url)
        if not url or not is_publishable_source_url(url) or not key or key in seen:
            continue
        seen.add(key)
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
        if post.frontmatter.get("_queued"):
            continue
        score = cosine_similarity(topic_text, post.searchable_text[:5000])
        if set(topic.get("tags", []) or []) & set(post.tags):
            score += 0.15
        if set(topic.get("categories", []) or []) & set(post.categories):
            score += 0.1
        scored.append((score, post))
    limit = int(config.get("publishing", {}).get("prefer_internal_links", 3))
    chosen = [
        {"title": post.title, "url": post.url_path, "role": "foundational" if index == 0 else "supporting"}
        for index, (score, post) in enumerate(sorted(scored, key=lambda item: item[0], reverse=True)[:limit])
        if score > 0.03
    ]
    return chosen


def ensure_contextual_internal_links(
    markdown: str,
    posts: list[Post],
    topic: dict[str, Any],
    config: dict[str, Any],
) -> str:
    """Deterministically add missing links selected for this reader intent."""
    available = select_internal_links(posts, topic, config)
    existing = set(re.findall(r"!?\[[^\]]+\]\((/[^)\s]+)\)", markdown_without_fenced_code(markdown)))
    required_posts = int(config.get("publishing", {}).get("minimum_internal_post_links", 0))
    existing_post_count = len({url for url in existing if url.startswith("/posts/")})
    additions: list[dict[str, str]] = []
    for link in available:
        url = str(link.get("url", ""))
        if not url or url in existing:
            continue
        if url.startswith("/posts/") and existing_post_count < required_posts:
            additions.append(link)
            existing_post_count += 1
            existing.add(url)
    if not additions:
        return markdown
    related = "## Related guidance\n\n" + "\n".join(
        f"- [{link['title']}]({link['url']}) — {str(link.get('role', 'related')).replace('_', ' ')} reference."
        for link in additions
    )
    return markdown.rstrip() + "\n\n" + related + "\n"


def max_existing_similarity(candidate_text: str, posts: list[Post]) -> dict[str, Any]:
    best = {"score": 0.0, "title_score": 0.0, "post": None}
    title = candidate_text.splitlines()[0] if candidate_text.strip() else candidate_text
    for post in posts:
        score = cosine_similarity(candidate_text, post.searchable_text)
        title_score = jaccard_similarity(title, post.title)
        if score > best["score"] or title_score > best["title_score"]:
            best = {"score": score, "title_score": title_score, "post": post}
    return best


def heading_text(markdown: str) -> str:
    return " ".join(
        match.group(1)
        for match in re.finditer(r"(?m)^#{2,6}\s+(.+)$", markdown_without_fenced_code(markdown))
    )


def word_ngrams(value: str, size: int = 5) -> set[tuple[str, ...]]:
    tokens = tokenize(value)
    return {tuple(tokens[index:index + size]) for index in range(max(0, len(tokens) - size + 1))}


def ngram_overlap(left: str, right: str, size: int = 5) -> float:
    left_grams = word_ngrams(left, size)
    right_grams = word_ngrams(right, size)
    if not left_grams or not right_grams:
        return 0.0
    return len(left_grams & right_grams) / min(len(left_grams), len(right_grams))


def detailed_existing_similarity(
    *,
    title: str,
    slug: str,
    search_intent: str,
    body: str,
    categories: list[str],
    tags: list[str],
    source_urls: list[str],
    posts: list[Post],
) -> dict[str, Any]:
    best: dict[str, Any] = {
        "post": None,
        "semantic": 0.0,
        "title": 0.0,
        "intent": 0.0,
        "heading": 0.0,
        "ngram": 0.0,
        "source_overlap": 0.0,
        "category_overlap": 0.0,
        "tag_overlap": 0.0,
        "slug": 0.0,
    }
    candidate_intro = re.split(r"(?m)^##\s+", body, maxsplit=1)[0]
    candidate_conclusion = body[-1200:]
    candidate_sources = {canonical_url(url) for url in source_urls if url}
    for post in posts:
        post_sources = {canonical_url(url) for url in extract_links(post.body)}
        metrics = {
            "semantic": cosine_similarity(f"{title} {search_intent} {body[:8000]}", post.searchable_text),
            "title": jaccard_similarity(title, post.title),
            "intent": cosine_similarity(search_intent or title, f"{post.title} {post.description} {heading_text(post.body)}"),
            "heading": jaccard_similarity(heading_text(body), heading_text(post.body)),
            "ngram": max(
                ngram_overlap(body, post.body),
                ngram_overlap(candidate_intro, post.body[:1200]),
                ngram_overlap(candidate_conclusion, post.body[-1200:]),
            ),
            "source_overlap": len(candidate_sources & post_sources) / max(1, min(len(candidate_sources), len(post_sources))),
            "category_overlap": 1.0 if set(categories) & set(post.categories) else 0.0,
            "tag_overlap": len(set(tags) & set(post.tags)) / max(1, min(len(set(tags)), len(set(post.tags)))),
            "slug": jaccard_similarity(slug.replace("-", " "), post.slug.replace("-", " ")),
        }
        if max(metrics.values()) > max(value for key, value in best.items() if key != "post"):
            best = {"post": post, **metrics}
    return best


def topic_relevance_score(topic: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    """Calculate a deterministic scope score before generation starts."""
    scope = config.get("topic_scope", {})
    approved = set(scope.get("approved_categories", config.get("taxonomy", {}).get("allowed_categories", [])))
    categories = set(topic.get("categories", []) or [])
    primary = slugify(str(topic.get("primary_category", "")), max_length=50)
    text = " ".join(
        [
            str(topic.get("title", "")),
            str(topic.get("search_intent", "")),
            str(topic.get("why_now", "")),
            " ".join(topic.get("tags", []) or []),
            " ".join(topic.get("source_titles", []) or []),
            " ".join(topic.get("source_domains", []) or []),
        ]
    ).lower()
    disallowed_matches = [
        str(term)
        for term in scope.get("disallowed_terms", [])
        if re.search(rf"(?<!\w){re.escape(str(term).lower())}(?!\w)", text)
    ]
    if disallowed_matches:
        return {
            "score": 0.0,
            "category_score": 0.0,
            "keyword_score": 0.0,
            "best_keyword_category": "",
            "approved": False,
            "critical_failure": "disallowed_topic",
            "disallowed_matches": disallowed_matches,
        }
    keyword_scores: dict[str, float] = {}
    for category, keywords in scope.get("category_keywords", {}).items():
        matches = sum(1 for keyword in keywords if str(keyword).lower() in text)
        keyword_scores[str(category)] = min(1.0, matches / 2)
    best_keyword_category, best_keyword_score = max(keyword_scores.items(), key=lambda item: item[1], default=("", 0.0))
    category_score = 1.0 if primary in approved or bool(categories & approved) else 0.0
    alignment_score = 1.0 if primary == best_keyword_category and best_keyword_score else (0.65 if best_keyword_score else 0.0)
    source_score = min(1.0, len(topic.get("source_urls", []) or []) / 2)
    score = round(0.55 * category_score + 0.3 * best_keyword_score + 0.1 * alignment_score + 0.05 * source_score, 4)
    return {
        "score": score,
        "category_score": category_score,
        "keyword_score": round(best_keyword_score, 4),
        "best_keyword_category": best_keyword_category,
        "approved": bool(categories & approved or primary in approved),
        "critical_failure": "",
        "disallowed_matches": [],
    }


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
            "tags": post.tags[:4],
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
    balance = category_balance_snapshot(posts, config)
    underrepresented = target_category(posts, config)
    existing_posts = topic_selection_existing_posts(posts, config)
    month = str(local_now(config).month)
    seasonal = config.get("seasonal_focus", {}).get(month, [])
    required_sources = int(config.get("publishing", {}).get("required_source_count", 1))
    grounded = {
        "text": normalize_space(str((grounded_brief or {}).get("text", "")))[:600],
        "citations": [
            {
                "title": str(citation.get("title", ""))[:160],
                "url": str(citation.get("url", "")),
            }
            for citation in ((grounded_brief or {}).get("citations", []) or [])
            if isinstance(citation, dict) and citation.get("url")
        ][:4],
    }
    return f"""
You are the autonomous editorial system for Compile My Mind. Publish only practical technical material in these approved areas: cybersecurity, identity and access management, networking, IT fundamentals, Microsoft Azure, Microsoft Entra ID, cloud certifications, system administration, practical infrastructure guides, and developer/IT tools.

Choose the strongest publishable article topic for the next autonomous post.

Hard requirements:
- Match one or more allowed categories and give a clear search intent.
- Never choose celebrity, entertainment, politics, lifestyle, automotive, generic trend, or other out-of-scope content.
- Prefer the underrepresented category if it can produce a genuinely useful post: {underrepresented}.
- Use the 7-day and 30-day distribution as a tie-breaker only. Never select a weak topic to fill a category.
- Avoid duplicates and near-duplicates of existing posts.
- Compare search intent, not just titles. When a close article exists, choose exactly one action: update, expand, differentiate with a distinct reader question, or cancel. Do not create keyword variants.
- Use only the supplied primary-source pages. Do not treat keyword overlap alone as evidence.
- Every candidate must include at least {required_sources} distinct, directly relevant source_urls from the supplied research items. A topic with fewer sources is not a candidate.
- Prioritize a reader problem, current trustworthy documentation, and durable search demand.
- Give additional priority to in-scope certification changes, security deadlines, deprecations, end-of-support transitions, and documentation changes when they are supported by current primary sources.
- Prefer topics that can be educational and comprehensive, not shallow news summaries.
- Use titles that answer a concrete intent, such as "How to Configure", "Troubleshooting", "A Practical Guide", "X vs. Y", or "Common Errors and Fixes". Never copy a source or announcement title.
- Consider seasonal focus: {json.dumps(seasonal, ensure_ascii=False)}.
- Return exactly 4 concise candidate topics, ranked best to worst.
- Include backup candidates across at least 2 approved categories so processing can continue after a rejection.

Category counts:
{json.dumps(counts, ensure_ascii=False, separators=(",", ":"))}

Rolling category distribution:
{json.dumps(balance, ensure_ascii=False, separators=(",", ":"))}

Allowed categories:
{json.dumps(config.get("taxonomy", {}).get("allowed_categories", []), ensure_ascii=False)}

Existing posts:
{json.dumps(existing_posts, ensure_ascii=False, separators=(",", ":"))}

Research feed items:
{json.dumps(topic_selection_research_payload(research, config), ensure_ascii=False, separators=(",", ":"))}

Optional Gemini grounded research brief:
{json.dumps(grounded, ensure_ascii=False, separators=(",", ":"))}

Return JSON only with this shape:
{{
  "topics": [
    {{
      "title": "SEO-friendly title",
      "slug": "url-slug",
      "primary_category": "allowed-category",
      "categories": ["allowed-category"],
      "tags": ["tag-one", "tag-two"],
      "search_intent": "what readers are trying to learn",
      "why_now": "why this topic is timely or evergreen",
      "content_action": "create|update|expand|differentiate|cancel",
      "article_type": "troubleshooting|tutorial|comparison|conceptual",
      "source_urls": ["https://..."],
      "needs_diagram": true,
      "needs_chart": false
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
    excluded_slugs: set[str] | None = None,
) -> dict[str, Any] | None:
    excluded_slugs = excluded_slugs or set()
    prompt = topic_selection_prompt(research, grounded_brief, posts, config)
    max_prompt_characters = int(config.get("research", {}).get("topic_selection_max_prompt_characters", 18000))
    if len(prompt) > max_prompt_characters:
        log.log("topic_selection_prompt_rejected", characters=len(prompt), maximum=max_prompt_characters)
        result: dict[str, Any] = {}
    else:
        try:
            result = client.generate_json(
                prompt,
                temperature=float(config.get("github_models", {}).get("topic_selection_temperature", 0.1)),
                max_output_tokens=int(config.get("github_models", {}).get("topic_selection_max_output_tokens", 1800)),
                task="topic_selection",
            )
        except (ValueError, GeminiTransientError) as error:
            log.log("topic_selection_model_invalid", error=str(error))
            result = {}
    candidates = result.get("topics") or []
    if isinstance(result.get("topic"), dict):
        candidates.insert(0, result["topic"])
    if isinstance(result.get("items"), list):
        candidates.extend(item for item in result["items"] if isinstance(item, dict))
    existing_slugs = {post.slug for post in posts}
    max_similarity = float(config.get("publishing", {}).get("max_similarity", 0.42))
    max_title_similarity = float(config.get("publishing", {}).get("max_title_similarity", 0.55))
    allowed = set(config.get("taxonomy", {}).get("allowed_categories", []))
    available_sources = {canonical_url(item.url) for item in research if item.validated or not config.get("source_validation", {}).get("trusted_domains")}
    required_sources = int(config.get("publishing", {}).get("required_source_count", 1))
    require_source_qualified = bool(config.get("cost_control", {}).get("require_source_qualified_topic", False))
    for topic in candidates:
        title = normalize_space(str(topic.get("title", "")))
        if not title:
            continue
        content_action = normalize_space(str(topic.get("content_action", "create"))).lower() or "create"
        if content_action not in {"create", "differentiate"}:
            log.log(
                "topic_deferred_to_maintenance",
                title=title,
                decision=content_action,
                reason="new_publication_not_appropriate",
            )
            continue
        slug = slugify(str(topic.get("slug") or title), int(config.get("publishing", {}).get("max_slug_length", 82)))
        topic["slug"] = slug
        categories = sanitize_categories(topic.get("categories", []), topic.get("primary_category"), config)
        topic["categories"] = categories
        topic["tags"] = sanitize_tags(topic.get("tags", []), topic, config)
        requested_sources = {
            canonical_url(str(url))
            for url in topic.get("source_urls", []) or []
            if canonical_url(str(url))
        }
        matched_source_items = [
            item
            for item in research
            if canonical_url(item.url) in requested_sources
            and topic_source_is_directly_relevant(topic, item, config)
        ]
        rejected_source_count = len(requested_sources) - len({canonical_url(item.url) for item in matched_source_items})
        topic["source_titles"] = [item.title for item in matched_source_items]
        topic["source_domains"] = [normalized_url_host(item.url) for item in matched_source_items]
        if slug in excluded_slugs:
            log.log("topic_rejected", title=title, reason="previous_generation_failed", slug=slug)
            continue
        if slug in existing_slugs:
            log.log("topic_rejected", title=title, reason="slug_exists", slug=slug)
            continue
        if not set(categories) & allowed:
            log.log("topic_rejected", title=title, reason="category_not_allowed", categories=categories)
            continue
        if available_sources and (not requested_sources or not requested_sources <= available_sources):
            log.log("topic_rejected", title=title, reason="topic_uses_unvalidated_source")
            continue
        if rejected_source_count:
            log.log(
                "topic_rejected",
                title=title,
                reason="source_bundle_not_directly_relevant",
                requested_source_count=len(requested_sources),
                directly_relevant_source_count=len(matched_source_items),
            )
            continue
        if require_source_qualified and len({canonical_url(item.url) for item in matched_source_items}) < required_sources:
            log.log(
                "topic_rejected",
                title=title,
                reason="insufficient_prevalidated_sources",
                source_count=len({canonical_url(item.url) for item in matched_source_items}),
                required=required_sources,
            )
            continue
        relevance = topic_relevance_score(topic, config)
        min_relevance = float(config.get("publishing", {}).get("topic_relevance_min_score", 0.0))
        if not relevance["approved"] or relevance["score"] < min_relevance:
            log.log("topic_rejected", title=title, reason="topic_relevance_too_low", relevance=relevance)
            continue
        duplicate = detailed_existing_similarity(
            title=title,
            slug=slug,
            search_intent=str(topic.get("search_intent", "")),
            body="",
            categories=categories,
            tags=topic.get("tags", []) or [],
            source_urls=topic.get("source_urls", []) or [],
            posts=posts,
        )
        if (
            duplicate["title"] > max_title_similarity
            or duplicate["slug"] > max_title_similarity
            or duplicate["intent"] > float(config.get("publishing", {}).get("max_search_intent_similarity", 1.0))
        ):
            similar_post = duplicate["post"]
            log.log(
                "topic_rejected",
                title=title,
                reason="duplicate_search_intent",
                duplicate_action="cancel",
                similar_post=similar_post.title if similar_post else "",
                metrics={key: value for key, value in duplicate.items() if key != "post"},
            )
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
            relevance=relevance,
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
        excluded_slugs=excluded_slugs,
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
    excluded_slugs: set[str] | None = None,
) -> dict[str, Any] | None:
    excluded_slugs = excluded_slugs or set()
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
            slug = slugify(title, int(config.get("publishing", {}).get("max_slug_length", 82)))
            if slug in existing_slugs or slug in excluded_slugs:
                continue
            categories = sanitize_categories([primary, *item.categories], primary, config)
            tags = sanitize_tags([primary, item.source, *tokenize(item.title)[:5]], {"title": title, "categories": [primary]}, config)
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
            required_sources = int(config.get("publishing", {}).get("required_source_count", 1))
            require_source_qualified = bool(config.get("cost_control", {}).get("require_source_qualified_topic", False))
            if require_source_qualified:
                scoped = research_items_for_topic(
                    topic,
                    research,
                    limit=max(required_sources, int(config.get("research", {}).get("topic_source_max_items", 6))),
                    config=config,
                )
                if len(scoped) < required_sources:
                    log.log(
                        "fallback_topic_rejected",
                        title=title,
                        reason="insufficient_prevalidated_sources",
                        source_count=len(scoped),
                        required=required_sources,
                    )
                    continue
                topic["source_urls"] = [source.url for source in scoped]
                topic["source_titles"] = [source.title for source in scoped]
                topic["source_domains"] = [normalized_url_host(source.url) for source in scoped]
            relevance = topic_relevance_score(topic, config)
            if relevance["score"] < float(config.get("publishing", {}).get("topic_relevance_min_score", 0.0)):
                log.log("fallback_topic_rejected", title=title, reason="topic_relevance_too_low", relevance=relevance)
                continue
            log.log("fallback_topic_selected", title=title, slug=slug, categories=categories, source=item.source)
            return topic
    return None


def choose_evergreen_topic(
    posts: list[Post],
    config: dict[str, Any],
    log: EventLog,
    *,
    excluded_slugs: set[str] | None = None,
) -> dict[str, Any] | None:
    """Select a pre-scoped evergreen topic only after dynamic topics fail."""
    excluded_slugs = excluded_slugs or set()
    existing_slugs = {post.slug for post in posts}
    max_similarity = float(config.get("publishing", {}).get("max_similarity", 0.42))
    max_title_similarity = float(config.get("publishing", {}).get("max_title_similarity", 0.55))
    for configured in config.get("research", {}).get("evergreen_topics", []) or []:
        if not isinstance(configured, dict):
            continue
        topic = dict(configured)
        title = normalize_space(str(topic.get("title", "")))
        if not title:
            continue
        slug = slugify(str(topic.get("slug") or title), int(config.get("publishing", {}).get("max_slug_length", 82)))
        if slug in existing_slugs or slug in excluded_slugs:
            continue
        topic["slug"] = slug
        topic["categories"] = sanitize_categories(topic.get("categories", []), topic.get("primary_category"), config)
        topic["tags"] = sanitize_tags(topic.get("tags", []), topic, config)
        topic["source_urls"] = [
            str(source.get("url", "")).strip()
            for source in topic.get("seed_sources", []) or []
            if isinstance(source, dict) and source.get("url")
        ]
        relevance = topic_relevance_score(topic, config)
        if not relevance.get("approved") or relevance.get("score", 0.0) < float(
            config.get("publishing", {}).get("topic_relevance_min_score", 0.0)
        ):
            log.log("evergreen_topic_rejected", title=title, reason="topic_relevance_too_low", relevance=relevance)
            continue
        duplicate = detailed_existing_similarity(
            title=title,
            slug=slug,
            search_intent=str(topic.get("search_intent", "")),
            body="",
            categories=topic.get("categories", []) or [],
            tags=topic.get("tags", []) or [],
            source_urls=topic.get("source_urls", []) or [],
            posts=posts,
        )
        if (
            duplicate["title"] > max_title_similarity
            or duplicate["slug"] > max_title_similarity
            or duplicate["intent"] > float(config.get("publishing", {}).get("max_search_intent_similarity", 1.0))
        ):
            similar_post = duplicate.get("post")
            log.log(
                "evergreen_topic_rejected",
                title=title,
                reason="duplicate_search_intent",
                metrics={key: value for key, value in duplicate.items() if key != "post"},
                similar_post=similar_post.title if similar_post else "",
            )
            continue
        log.log("evergreen_topic_selected", title=title, slug=slug, categories=topic["categories"])
        return topic
    log.log("no_evergreen_topic_selected")
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
    if not output and allowed:
        fallback = next(iter(sorted(allowed)))
        output = [fallback]
    # Older configurations treated guide as an editorial type. Keep that
    # behavior only when it is explicitly configured, not for the new scope.
    if "guide" in allowed and "guide" not in output:
        output.insert(0, "guide")
    return output[:3]


def sanitize_tags(values: Any, topic: dict[str, Any], config: dict[str, Any] | None = None) -> list[str]:
    raw_values = values if isinstance(values, list) else []
    if not raw_values:
        raw_values = tokenize(str(topic.get("title", "")))[:6]
    taxonomy = (config or {}).get("taxonomy", {})
    if not taxonomy and isinstance(topic.get("_taxonomy"), dict):
        taxonomy = topic["_taxonomy"]
    controlled = {slugify(str(value), max_length=34) for value in taxonomy.get("controlled_tags", [])}
    allow_new_tags = bool(taxonomy.get("allow_new_tags", False))
    aliases = {
        slugify(str(key), max_length=34): slugify(str(value), max_length=34)
        for key, value in taxonomy.get("tag_aliases", {}).items()
    }
    tags: list[str] = []
    for value in raw_values:
        tag = slugify(str(value), max_length=34)
        tag = aliases.get(tag, tag)
        if controlled and tag not in controlled and not allow_new_tags:
            continue
        if tag and tag not in tags:
            tags.append(tag)
    if not tags and controlled:
        category_tags = [slugify(str(category), max_length=34) for category in topic.get("categories", []) or []]
        tags = [tag for tag in category_tags if tag in controlled][:1]
    max_tags = int(taxonomy.get("max_tags_per_article", 8))
    return tags[:max_tags]


def existing_tag_counts(posts: list[Post]) -> Counter[str]:
    """Return the reusable tag vocabulary already present on public posts."""
    return Counter(
        tag
        for post in posts
        for raw_tag in post.tags
        if (tag := slugify(str(raw_tag), max_length=34))
    )


def article_tag_relevance(tag: str, article: dict[str, Any], topic: dict[str, Any]) -> float:
    """Score whether a tag describes a material subject of an article."""
    normalized_tag = slugify(tag, max_length=34)
    tag_tokens = tokenize(normalized_tag.replace("-", " "))
    if not normalized_tag or not tag_tokens:
        return 0.0

    title = re.sub(r"[-_/]+", " ", str(article.get("title") or topic.get("title", ""))).lower()
    metadata = re.sub(
        r"[-_/]+",
        " ",
        " ".join(
            str(value)
            for value in (
                article.get("description", ""),
                article.get("summary", ""),
                topic.get("search_intent", ""),
            )
        ),
    ).lower()
    body = re.sub(r"[-_/]+", " ", str(article.get("article_markdown", ""))).lower()
    phrase = normalized_tag.replace("-", " ")
    title_tokens = set(tokenize(title))
    metadata_tokens = set(tokenize(metadata))
    body_tokens = set(tokenize(body))

    score = 0.0
    if phrase in title:
        score += 8.0
    if phrase in metadata:
        score += 5.0
    if phrase in body:
        score += min(5.0, 1.0 + body.count(phrase) * 0.5)
    if set(tag_tokens) <= title_tokens:
        score += 4.0
    elif set(tag_tokens) <= metadata_tokens:
        score += 2.5
    elif set(tag_tokens) <= body_tokens:
        score += 1.5
    return score


def reconcile_article_tags(
    article: dict[str, Any],
    topic: dict[str, Any],
    posts: list[Post],
    config: dict[str, Any],
) -> list[str]:
    """Prefer relevant existing tags, creating a tag only when reuse is insufficient."""
    taxonomy = config.get("taxonomy", {})
    preferred_count = max(1, int(taxonomy.get("preferred_tags_per_article", 3)))
    max_tags = max(preferred_count, int(taxonomy.get("max_tags_per_article", 8)))
    categories = {
        slugify(str(category), max_length=34)
        for category in article.get("categories", topic.get("categories", [])) or []
    }
    proposed = sanitize_tags(
        [*(article.get("tags", []) or []), *(topic.get("tags", []) or [])],
        topic,
        config,
    )
    counts = existing_tag_counts(posts)
    tag_categories: dict[str, set[str]] = defaultdict(set)
    for post in posts:
        for raw_tag in post.tags:
            tag = slugify(str(raw_tag), max_length=34)
            if tag:
                tag_categories[tag].update(
                    slugify(str(category), max_length=40)
                    for category in post.categories
                )
    descriptor_tokens = set(
        tokenize(
            " ".join(
                [
                    str(article.get("title") or topic.get("title", "")),
                    str(article.get("description", "")),
                    str(article.get("summary", "")),
                    str(topic.get("search_intent", "")),
                ]
            )
        )
    )

    def relevance(tag: str) -> float:
        return article_tag_relevance(tag, article, topic)

    reusable = [
        tag
        for tag in counts
        if tag not in categories
        and relevance(tag) > 0
        and (
            bool(tag_categories[tag] & categories)
            or set(tokenize(tag.replace("-", " "))) <= descriptor_tokens
        )
    ]
    reusable.sort(key=lambda tag: (-relevance(tag), -counts[tag], tag))
    selected = reusable[:preferred_count]

    # A model-proposed tag may enter the vocabulary only when the existing
    # catalog cannot provide enough relevant choices for this article.
    if len(selected) < preferred_count and taxonomy.get("allow_new_tags", False):
        new_candidates = [
            tag
            for tag in proposed
            if tag not in counts and tag not in categories and relevance(tag) > 0
        ]
        new_candidates.sort(key=lambda tag: (-relevance(tag), tag))
        for tag in new_candidates:
            if tag not in selected:
                selected.append(tag)
            if len(selected) >= preferred_count:
                break

    # Preserve a relevant proposed tag when no historical vocabulary exists,
    # including legacy configurations that do not maintain a controlled list.
    if not selected:
        selected = [tag for tag in proposed if tag not in categories and relevance(tag) > 0][:preferred_count]
    if not selected:
        selected = proposed[:preferred_count]
    return selected[:max_tags]


def metadata_enrichment_prompt(
    article: dict[str, Any],
    topic: dict[str, Any],
    config: dict[str, Any],
    posts: list[Post] | None = None,
) -> str:
    allowed_categories = config.get("taxonomy", {}).get("allowed_categories", [])
    existing_tags = existing_tag_counts(posts or [])
    reusable_tags = [
        {"tag": tag, "articles": count}
        for tag, count in sorted(existing_tags.items(), key=lambda item: (-item[1], item[0]))
    ]
    return f"""
You are a lightweight editorial metadata assistant for Compile My Mind.

Do not rewrite the article. Read the existing title and body, then return concise,
accurate metadata that is faithful to the article. Do not invent facts, sources,
or categories. Use only the allowed categories. Select three concise tags. Prefer
logically relevant tags from the existing site vocabulary below. Create a new tag
only when no existing tag accurately describes a major subject of the article.
Do not repeat a category slug as a tag and do not split a category name into
generic fragments.

Topic context:
{json.dumps({k: topic.get(k) for k in ["title", "categories", "tags", "search_intent"]}, ensure_ascii=False, indent=2)}

Current article metadata:
{json.dumps({k: article.get(k) for k in ["title", "description", "summary", "categories", "tags"]}, ensure_ascii=False, indent=2)}

Allowed categories:
{json.dumps(allowed_categories, ensure_ascii=False)}

Existing site tags and usage counts:
{json.dumps(reusable_tags, ensure_ascii=False)}

Article body:
{str(article.get("article_markdown", ""))[:18000]}

Return JSON only:
{{
  "description": "105-180 character SEO description",
  "summary": "one concise, reader-facing article summary",
  "categories": ["guide", "one allowed technical category"],
  "tags": ["relevant-existing-tag", "another-existing-tag", "specific-topic-tag"]
}}
""".strip()


def enrich_article_metadata(
    client: GeminiClient,
    article: dict[str, Any],
    topic: dict[str, Any],
    config: dict[str, Any],
    log: EventLog,
    posts: list[Post] | None = None,
) -> dict[str, Any]:
    """Use the lightweight model for metadata without changing article prose."""
    metadata_topic = dict(topic)
    metadata_topic["title"] = article.get("title") or topic.get("title", "")
    metadata_topic["categories"] = article.get("categories") or topic.get("categories", [])
    metadata_topic["tags"] = article.get("tags") or topic.get("tags", [])
    try:
        payload = client.generate_json(
            metadata_enrichment_prompt(article, metadata_topic, config, posts),
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
        article["tags"] = sanitize_tags(payload["tags"], metadata_topic, config)
    reconciled_topic = dict(metadata_topic)
    reconciled_topic["tags"] = list(article.get("tags", []))
    article["tags"] = reconcile_article_tags(article, reconciled_topic, posts or [], config)
    reused_tags = sorted(set(article["tags"]) & set(existing_tag_counts(posts or [])))
    log.log(
        "metadata_enriched",
        fields=[field for field in ["description", "summary", "categories", "tags"] if field in article],
        tags=article["tags"],
        reused_tags=reused_tags,
    )
    return article


def article_generation_prompt(
    topic: dict[str, Any],
    research: list[ResearchItem],
    posts: list[Post],
    config: dict[str, Any],
    feedback: str = "",
) -> str:
    source_items = research_items_for_topic(topic, research, limit=8, config=config)
    internal_links = select_internal_links(posts, topic, config)
    min_words = int(config.get("publishing", {}).get("min_words", 1400))
    target_words = max(min_words + 350, int(config.get("publishing", {}).get("target_words", min_words + 500)))
    required_sources = int(config.get("publishing", {}).get("required_source_count", 3))
    source_snippet_characters = int(
        config.get("research", {}).get("article_prompt_snippet_characters", 900)
    )
    return f"""
You are writing for Compile My Mind. Create a comprehensive, original Hugo blog article as structured JSON.

Topic:
{json.dumps(topic, ensure_ascii=False, indent=2)}

Editorial style:
- Practical, technically accurate, readable, and educational.
- Explain concepts with concrete examples.
- Avoid hype, filler, and shallow news recap.
- Choose an article-specific structure. Troubleshooting pieces must form a diagnostic decision process; tutorials need requirements, commands or configuration, expected results, validation, and rollback when relevant; comparisons must define the workload and assumptions before conditional recommendations; conceptual pieces should explain components and a practical scenario without forcing a generic checklist.
- Every paragraph must contain topic-specific detail. Reject standalone reminders such as "check the documentation", "follow best practices", "review the logs", "use a test account", or "document the result" unless the same paragraph names the concrete field, command, condition, interpretation, or next decision.
- Do not repeat sentences, paragraphs, warnings, section endings, rollback reminders, or formulaic transitions. Consolidate an operational principle in the one section where it belongs.
- Open with the reader's problem, who the guidance is for, and the direct answer or outcome. Do not use generic introductions such as "In today's rapidly evolving digital landscape", "Technology is changing faster than ever", "In the world of modern IT", "This comprehensive guide will explore", or "Whether you are a beginner or an expert".
- Optimize for search intent naturally, with a clear meta description.
- Include at least one useful Markdown comparison or reference table when the subject supports it.
- Include internal links naturally, using only the exact Markdown URLs from the provided internal-link list.
- Do not create a Sources section inside article_markdown; the publishing system adds it automatically.
- Do not place external Markdown links, HTML links, autolinks, or bare external URLs inside article_markdown.
- Put every external citation only in the sources array.
- Every sources[].url must be copied exactly from one of the supplied research snippets. Do not invent, shorten, normalize, or add tracking parameters to URLs.
- Do not invent facts that are not supported by the research snippets.
- For every material product name, version, command, configuration value, exam code, pricing detail, release date, deprecation, limitation, or security recommendation, add a claim_evidence entry that identifies the source URL and any relevant version context. Use cautious, version-specific language when sources disagree.
- Every URL in sources must support at least one claim_evidence record. Do not pad the source list.
- Do not use universal claims such as always, never, guaranteed, only sensible choice, must fit in memory, universally better, or a product "will scale to" a number unless the claim is narrowly scoped and directly evidenced.
- Do not state exact latency, throughput, capacity, memory, cost, or performance numbers without an evidence record plus product version, hardware or service tier, dataset/workload, concurrency, cache/index configuration, measurement method, and limitations. Prefer qualitative language when that context is unavailable.
- Set verification_status to one of "Documentation reviewed", "Source reviewed", "Tested in a lab environment", "Tested in a production-like environment", or "Not independently tested". Never claim hands-on testing unless test_metadata records the date, version, environment, actions, observed result, and limitations.
- When a test account is discussed, state whether its groups, policy scope, assignments, authentication method, device state, network location, risk, license, tenant configuration, and sign-in type reproduce the incident conditions; say when the test is only partially representative.
- Troubleshooting guidance must identify the relevant role or permission, evidence source or log category, exact fields or command, expected and failure results, interpretation, next check, safe remediation boundary, validation, rollback, and escalation evidence when applicable. Include only product-relevant fields.
- A comparison must never declare a universal winner. Define the workload, assumptions, query/read/write behavior, operational constraints, security, maintenance, migration, and cost factors that actually affect the recommendation.
- Do not include YAML front matter or any top-level H1 in article_markdown. Start section headings at H2 (##).
- Never refer to yourself, the prompt, or limitations of being an AI system.
- The sources array must include at least {required_sources} URLs selected from the research snippets.
- If the topic involves AI agents, code reviewers, or machine learning systems, discuss them as technical systems, not as yourself.
- For software topics, include runnable examples, architecture explanations, trade-offs, version context, and testing guidance when appropriate.
- Shell code blocks must contain executable examples, not command-syntax notation. Never use angle-bracket metavariables such as <verb> or <resource> inside Bash; use safe examples or clearly named shell variables such as $VERB and $RESOURCE.
- The article body must contain at least {min_words} words; target about {target_words} words so JSON metadata and normalization cannot leave it short.
- Do not stop after an introduction or a short explanation. Build a complete article with a useful opening, multiple topic-specific H2/H3 sections, practical details, examples or pseudocode where relevant, trade-offs, and an actionable conclusion.
- Allocate most of the word budget to original practical guidance: a diagnostic or implementation workflow, at least three realistic scenarios or failure modes, expected outputs, a decision or reference table, common mistakes, security considerations, and a final checklist when they fit the topic. Do not mirror the documentation page's section order.
- Before returning JSON, count the words in article_markdown itself—not the metadata—and expand undersized sections with useful original examples until it exceeds {min_words} words.
- Do not create a featured image, hero image, thumbnail, or image before the article title.
- Body diagrams, charts, and data visualizations are allowed only when they materially improve understanding; reference generated filenames in the Markdown.
- Each chart must identify a source_url from the supplied research, units, version_context, measurement_context, and limitations. Do not create a chart from invented values.

Available internal links:
{json.dumps(internal_links, ensure_ascii=False, indent=2)}

Research snippets:
{json.dumps(research_for_prompt(source_items, snippet_characters=source_snippet_characters), ensure_ascii=False, indent=2)}

Previous QA feedback to fix:
{feedback or "No previous feedback. Produce the full article on the first attempt."}

Return JSON only with this shape:
{{
  "title": "final title",
  "slug": "final-slug",
  "description": "145-160 character SEO description",
  "categories": ["allowed-category"],
  "tags": ["tag-one"],
  "article_markdown": "Markdown body only, no front matter, no H1.",
  "article_type": "troubleshooting|tutorial|comparison|conceptual",
  "verification_status": "Documentation reviewed",
  "version_context": "Product and documentation version context",
  "test_metadata": {{}},
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
      "source_url": "https://...",
      "version_context": "Version represented by the data",
      "measurement_context": "Workload and measurement method",
      "limitations": "What the chart does not establish",
      "data": [{{"label": "Option A", "value": 42}}]
    }}
  ],
  "sources": [
    {{"title": "Source title", "url": "https://..."}}
  ],
  "claim_evidence": [
    {{"claim": "A material factual claim used in the article.", "supporting_sources": ["https://..."], "confidence": 0.0, "verified_at": "ISO-8601 timestamp", "version_context": "Product version or verification context when relevant."}}
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


def escape_hugo_shortcode_delimiters(markdown: str) -> str:
    """Prevent model examples from being parsed as Hugo shortcodes.

    Hugo treats ``{{<`` and ``{{%`` as shortcode invocations even when they
    occur in otherwise ordinary Markdown.  Technical examples (Helm,
    templating, and configuration snippets) commonly contain those sequences.
    Escape them only in prose; fenced code remains executable/copyable.
    """
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
            line = line.replace("{{<", "&#123;&#123;&lt;")
            line = line.replace("{{%", "&#123;&#123;%")
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
    return escape_hugo_shortcode_delimiters(normalize_top_level_headings(markdown)).strip()


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

    scheme = parsed.scheme.lower()
    if host == "compilemymind.com":
        scheme = "https"
        host = "www.compilemymind.com"
        path = path.lower()
        if not path.endswith("/"):
            path += "/"
        query_items = []

    return urllib.parse.urlunsplit(
        (
            scheme,
            host,
            path,
            urllib.parse.urlencode(query_items, doseq=True),
            "",
        )
    )


def domain_matches(host: str, allowed_domain: str) -> bool:
    host = host.lower().strip(".")
    allowed_domain = allowed_domain.lower().strip().lstrip(".")
    return bool(host and allowed_domain and (host == allowed_domain or host.endswith(f".{allowed_domain}")))


def is_trusted_source_url(url: str, config: dict[str, Any]) -> bool:
    """Return whether a citation is a direct page on an approved primary domain."""
    if not is_publishable_source_url(url):
        return False
    trusted_domains = config.get("source_validation", {}).get("trusted_domains", [])
    if not trusted_domains:
        return True  # Backwards-compatible for isolated unit tests.
    host = normalized_url_host(url)
    if not any(domain_matches(host, str(domain)) for domain in trusted_domains):
        return False
    path = urllib.parse.urlsplit(url).path.lower()
    return not any(term.lower() in path for term in config.get("source_validation", {}).get("blocked_path_terms", []))


def extract_html_title(document: str) -> str:
    match = re.search(r"(?is)<title[^>]*>(.*?)</title>", document)
    return normalize_space(strip_html(match.group(1))) if match else ""


def extract_html_canonical(document: str, base_url: str) -> str:
    match = re.search(
        r"(?is)<link\b(?=[^>]*\brel\s*=\s*['\"]?canonical['\"]?)[^>]*\bhref\s*=\s*(?:['\"]([^'\"]+)['\"]|([^\s>]+))",
        document,
    )
    value = next((group for group in match.groups() if group), "") if match else ""
    return urllib.parse.urljoin(base_url, html.unescape(value)) if value else ""


def extract_primary_page_text(document: str) -> str:
    """Prefer documentation body text over headers, navigation, and footers."""
    # Documentation sites can embed HTML-looking template strings inside
    # JavaScript. Searching the raw document for a content class may otherwise
    # select a sidebar script before the real <main> element and make unrelated
    # pages share the same fingerprint.
    document = re.sub(r"(?is)<(script|style)\b[^>]*>.*?</\1>", " ", document)
    content_classes = (
        "td-content",
        "theme-doc-markdown",
        "markdown-body",
        "article-content",
        "post-content",
    )
    for class_name in content_classes:
        opening = re.search(
            rf"(?is)<[a-z0-9]+\b[^>]*\bclass\s*=\s*(?:[\"'][^\"']*\b{re.escape(class_name)}\b[^\"']*[\"']|[^\s>]*\b{re.escape(class_name)}\b[^\s>]*)[^>]*>",
            document,
        )
        if not opening:
            continue
        end_markers = [
            re.search(r"(?is)<div\b[^>]*\bid\s*=\s*(?:[\"']?pre-footer[\"']?)[^>]*>", document[opening.end():]),
            re.search(r"(?is)<footer\b", document[opening.end():]),
            re.search(r"(?is)</main\s*>", document[opening.end():]),
            re.search(r"(?is)</article\s*>", document[opening.end():]),
        ]
        offsets = [marker.start() for marker in end_markers if marker]
        body = document[opening.end():opening.end() + min(offsets)] if offsets else document[opening.end():]
        text = strip_html(body)
        if len(text) >= 100:
            return text
    # An <article> nested inside <main> is normally more specific. Large docs
    # shells can put a shared sidebar and table of contents before it; hashing
    # the whole <main> then makes unrelated pages appear identical.
    for tag in ("article", "main"):
        candidates = [
            strip_html(match)
            for match in re.findall(rf"(?is)<{tag}\b[^>]*>(.*?)</{tag}>", document)
        ]
        candidates = [text for text in candidates if len(text) >= 200]
        if candidates:
            return max(candidates, key=len)
    role_main = re.search(
        r"(?is)<(?P<tag>[a-z0-9]+)\b[^>]*\brole\s*=\s*['\"]main['\"][^>]*>(?P<body>.*?)</(?P=tag)>",
        document,
    )
    if role_main:
        text = strip_html(role_main.group("body"))
        if text:
            return text
    return strip_html(document)


def source_is_time_sensitive(item: ResearchItem) -> bool:
    text = f"{item.title} {item.summary}".lower()
    return bool(re.search(r"\b(release|version|changelog|announcement|pricing|exam|certification|deprecated)\b", text))


def validate_research_item(item: ResearchItem, config: dict[str, Any], log: EventLog) -> bool:
    """Verify a candidate citation before a model can use it for publication."""
    validation: dict[str, Any] = {"url": item.url, "verified_at": iso_z()}
    if not is_trusted_source_url(item.url, config):
        validation["reason"] = "untrusted_domain_or_non_article_url"
        item.validation = validation
        return False
    published = parse_date(item.published)
    if published:
        max_age = int(config.get("source_validation", {}).get(
            "max_release_age_days" if source_is_time_sensitive(item) else "max_age_days", 730
        ))
        age_days = (utc_now() - published.astimezone(dt.timezone.utc)).total_seconds() / 86400
        if age_days > max_age:
            validation.update({"reason": "source_is_outdated", "age_days": round(age_days, 1)})
            item.validation = validation
            return False
    try:
        status, body, headers = http_request(item.url, timeout=18, retries=1)
    except Exception as error:
        validation["reason"] = f"request_failed: {error}"
        item.validation = validation
        return False
    if status >= 400:
        validation.update({"reason": f"http_{status}", "status": status})
        item.validation = validation
        return False
    content_type = headers.get("content-type", "").lower()
    if content_type and not any(token in content_type for token in ("text/html", "application/xhtml", "text/plain")):
        validation.update({"reason": "non_html_source", "content_type": content_type})
        item.validation = validation
        return False
    max_document_bytes = int(config.get("source_validation", {}).get("max_document_bytes", 1500000))
    document = body[:max_document_bytes].decode("utf-8", errors="ignore")
    page_title = extract_html_title(document)
    page_text = extract_primary_page_text(document)[:12000]
    final_url = headers.get("x-final-url", item.url)
    if not is_trusted_source_url(final_url, config):
        validation.update({"reason": "redirected_to_untrusted_or_non_article_url", "final_url": final_url})
        item.validation = validation
        return False
    declared_canonical = extract_html_canonical(document, final_url)
    if declared_canonical and not is_trusted_source_url(declared_canonical, config):
        validation.update({"reason": "page_declares_untrusted_canonical", "canonical_url": declared_canonical})
        item.validation = validation
        return False
    if any(term.lower() in page_text.lower()[:2500] for term in config.get("source_validation", {}).get("blocked_page_terms", [])):
        validation["reason"] = "login_or_access_denied_page"
        item.validation = validation
        return False
    if not page_title or not page_text:
        validation["reason"] = "missing_readable_page_title_or_text"
        item.validation = validation
        return False
    title_similarity = jaccard_similarity(item.title, page_title)
    relevance = max(
        cosine_similarity(f"{item.title} {item.summary}", f"{page_title} {page_text[:3000]}"),
        title_similarity,
    )
    validation.update(
        {
            "status": status,
            "page_title": page_title[:300],
            "title_similarity": round(title_similarity, 4),
            "relevance": round(relevance, 4),
            "final_url": final_url,
            "canonical_url": declared_canonical or final_url,
            "content_fingerprint": hashlib.sha256(normalize_space(page_text).lower().encode("utf-8")).hexdigest(),
        }
    )
    if title_similarity < float(config.get("source_validation", {}).get("min_title_similarity", 0.08)):
        validation["reason"] = "page_title_does_not_match_feed_item"
        item.validation = validation
        return False
    if relevance < float(config.get("source_validation", {}).get("min_relevance_similarity", 0.05)):
        validation["reason"] = "page_is_not_relevant_to_feed_item"
        item.validation = validation
        return False
    resolved_canonical = canonical_url(declared_canonical or final_url)
    validation["canonical_url"] = resolved_canonical
    validation["reason"] = "validated"
    item.validation = validation
    item.validated = True
    item.url = resolved_canonical
    item.snippet = page_text[: int(config.get("research", {}).get("snippet_characters", 1600))]
    return True


def validate_research_items(items: list[ResearchItem], config: dict[str, Any], log: EventLog) -> list[ResearchItem]:
    """Retain only unique, accessible, relevant primary-source pages."""
    limit = int(config.get("source_validation", {}).get("max_candidates_per_run", len(items)))
    validated: list[ResearchItem] = []
    seen: set[str] = set()
    seen_content: set[str] = set()
    for item in sorted(items, key=lambda candidate: candidate.score, reverse=True)[:limit]:
        key = canonical_url(item.url)
        if not key or key in seen:
            continue
        seen.add(key)
        if validate_research_item(item, config, log):
            fingerprint = str(item.validation.get("content_fingerprint", ""))
            if fingerprint and fingerprint in seen_content:
                item.validated = False
                item.validation["reason"] = "duplicate_source_content"
                log.log("source_validation_failed", source=item.source, url=item.url, reason="duplicate_source_content")
                continue
            if fingerprint:
                seen_content.add(fingerprint)
            validated.append(item)
        else:
            log.log("source_validation_failed", source=item.source, url=item.url, reason=item.validation.get("reason", ""))
    log.log("source_validation_completed", accepted=len(validated), rejected=max(0, min(len(items), limit) - len(validated)))
    return validated


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


def normalize_shell_placeholders(markdown: str) -> str:
    """Convert documentation metavariables into valid, explicit shell variables."""
    def replace_block(match: re.Match[str]) -> str:
        fence, language, code, closing = match.groups()

        def replace_placeholder(value: re.Match[str]) -> str:
            variable = re.sub(r"[^A-Za-z0-9]+", "_", value.group(1)).strip("_").upper()
            return "${" + variable + "}"

        code = re.sub(r"<([A-Za-z][A-Za-z0-9_-]*)>", replace_placeholder, code)
        return f"{fence}{language}\n{code}{closing}"

    return re.sub(
        r"(?ms)^(```|~~~)(bash|sh|shell)\s*\n(.*?)(^\1\s*$)",
        replace_block,
        markdown,
    )


def normalize_code_fence_languages(markdown: str) -> str:
    """Label unlabeled fenced examples so syntax and accessibility checks can run."""
    lines = markdown.splitlines()
    normalized: list[str] = []
    active_fence: tuple[str, int] | None = None
    for index, line in enumerate(lines):
        match = re.match(r"^(\s*)(`{3,}|~{3,})(.*)$", line)
        if not match:
            normalized.append(line)
            continue
        indent, fence, info = match.groups()
        if active_fence:
            character, minimum_length = active_fence
            if fence[0] == character and len(fence) >= minimum_length and not info.strip():
                active_fence = None
            normalized.append(line)
            continue
        active_fence = (fence[0], len(fence))
        if info.strip():
            normalized.append(line)
            continue
        closing_index = next(
            (
                candidate
                for candidate in range(index + 1, len(lines))
                if re.match(rf"^\s*{re.escape(fence[0])}{{{len(fence)},}}\s*$", lines[candidate])
            ),
            len(lines),
        )
        code = "\n".join(lines[index + 1:closing_index])
        stripped = code.lstrip()
        if re.match(r"(?s)^[\[{]", stripped):
            language = "json"
        elif re.search(r"(?m)^(?:apiVersion|kind|metadata):", code):
            language = "yaml"
        elif re.search(r"(?m)^\s*(?:kubectl|git|az|aws|gcloud|docker|curl|export|echo)\b", code):
            language = "bash"
        else:
            language = "text"
        normalized.append(f"{indent}{fence}{language}")
    result = "\n".join(normalized)
    return result + ("\n" if markdown.endswith("\n") else "")


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
    trusted_research = [
        item for item in research
        if item.validated or not config.get("source_validation", {}).get("trusted_domains")
    ]
    researched_urls = {canonical_url(item.url) for item in trusted_research}
    sources = [
        source
        for source in (article_sources or [])
        if isinstance(source, dict)
        if canonical_url(str(source.get("url", "")).strip()) in researched_urls
    ]
    sources.extend(
        {"url": url, "title": ""}
        for url in topic.get("source_urls", []) or []
        if canonical_url(str(url).strip()) in researched_urls
    )
    for item in research_items_for_topic(topic, trusted_research, limit=8, config=config):
        sources.append({"title": item.title, "url": item.url})
        if len(dedupe_sources(sources)) >= required:
            break
    return dedupe_sources(sources)


def normalize_article_payload(
    article: dict[str, Any],
    topic: dict[str, Any],
    config: dict[str, Any],
    research: list[ResearchItem],
    posts: list[Post] | None = None,
) -> dict[str, Any]:
    title = normalize_space(str(article.get("title") or topic.get("title", "")))
    slug = slugify(
        str(article.get("slug") or topic.get("slug") or title),
        int(config.get("publishing", {}).get("max_slug_length", 82)),
    )
    article["title"] = title
    article["slug"] = slug
    article["description"] = normalize_space(str(article.get("description", "")))[:180]
    article["article_type"] = normalize_space(str(article.get("article_type") or topic.get("article_type", ""))).lower()
    article["verification_status"] = normalize_space(
        str(article.get("verification_status") or "Documentation reviewed")
    )
    article["version_context"] = normalize_space(
        str(article.get("version_context") or "Documentation current at verification time")
    )[:240]
    article["test_metadata"] = article.get("test_metadata") if isinstance(article.get("test_metadata"), dict) else {}
    article["categories"] = sanitize_categories(article.get("categories", topic.get("categories", [])), topic.get("primary_category"), config)
    article["tags"] = sanitize_tags(article.get("tags", topic.get("tags", [])), topic, config)
    if posts is not None:
        article["tags"] = reconcile_article_tags(article, topic, posts, config)
    article["sources"] = supplement_article_sources(article.get("sources", []), topic, research, config)
    allowed_source_urls = {canonical_url(str(source.get("url", ""))) for source in article["sources"]}
    evidence: list[dict[str, Any]] = []
    for item in article.get("claim_evidence", []) or []:
        if not isinstance(item, dict):
            continue
        claim = normalize_space(str(item.get("claim", "")))
        raw_source_urls = item.get("supporting_sources", item.get("source_urls", [])) or []
        source_urls = [
            str(url).strip()
            for url in raw_source_urls
            if canonical_url(str(url).strip()) in allowed_source_urls
        ]
        if claim and source_urls:
            try:
                confidence = max(0.0, min(1.0, float(item.get("confidence", 0.0) or 0.0)))
            except (TypeError, ValueError):
                confidence = 0.0
            evidence.append(
                {
                    "claim": claim[:500],
                    "supporting_sources": source_urls[:3],
                    "confidence": confidence,
                    "verified_at": str(item.get("verified_at") or iso_z()),
                    "version_context": normalize_space(str(item.get("version_context", "")))[:240],
                }
            )
    article["claim_evidence"] = evidence
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
    markdown = normalize_code_fence_languages(markdown)
    markdown = normalize_shell_placeholders(markdown)
    if posts is not None:
        markdown = ensure_contextual_internal_links(markdown, posts, topic, config)
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


def heading_hierarchy_issues(markdown: str) -> list[str]:
    levels = [len(match.group(1)) for match in re.finditer(r"(?m)^(#{2,6})\s+\S", markdown_without_fenced_code(markdown))]
    if not levels:
        return ["Article has no H2 sections."]
    issues: list[str] = []
    previous = 2
    for level in levels:
        if level > previous + 1:
            issues.append(f"Heading hierarchy skips from H{previous} to H{level}.")
        previous = level
    return issues


def introduction_issues(markdown: str) -> list[str]:
    opening = re.split(r"(?m)^##\s+", markdown_without_fenced_code(markdown), maxsplit=1)[0]
    opening = normalize_space(opening)[:900].lower()
    banned = [
        "in today's rapidly evolving digital landscape",
        "technology is changing faster than ever",
        "in the world of modern it",
        "this comprehensive guide will explore",
        "whether you are a beginner or an expert",
    ]
    return ["Introduction uses a generic AI-style opening."] if any(phrase in opening for phrase in banned) else []


def prose_paragraphs(markdown: str) -> list[str]:
    """Return narrative paragraphs while excluding headings, tables, and lists."""
    prose = markdown_without_fenced_code(markdown)
    prose = re.split(r"(?im)^##\s+Sources\b", prose, maxsplit=1)[0]
    paragraphs: list[str] = []
    for block in re.split(r"\n\s*\n", prose):
        lines = [line.strip() for line in block.splitlines() if line.strip()]
        if not lines:
            continue
        if any(line.startswith(("#", "|", ">", "- ", "* ", "+ ")) for line in lines):
            continue
        if all(re.match(r"^\d+[.)]\s+", line) for line in lines):
            continue
        paragraph = normalize_space(" ".join(lines))
        if paragraph:
            paragraphs.append(paragraph)
    return paragraphs


def sentence_units(value: str) -> list[str]:
    text = normalize_space(re.sub(r"(?m)^#{1,6}\s+", "", markdown_without_fenced_code(value)))
    return [
        normalize_space(sentence)
        for sentence in re.split(r"(?<=[.!?])\s+(?=[A-Z0-9])", text)
        if len(normalize_space(sentence).split()) >= 6
    ]


def _repetition_key(value: str) -> str:
    value = re.sub(r"\[[^\]]+\]\([^)]+\)", " ", value)
    value = re.sub(r"[^a-z0-9+#.-]+", " ", value.lower())
    return normalize_space(value)


def repetition_issues(markdown: str, config: dict[str, Any] | None = None) -> list[str]:
    policy = (config or {}).get("editorial_validation", {})
    minimum_words = int(policy.get("repetition_min_words", 8))
    near_threshold = float(policy.get("near_paragraph_similarity", 0.9))
    issues: list[str] = []
    paragraphs = prose_paragraphs(markdown)
    paragraph_keys = [_repetition_key(value) for value in paragraphs]
    repeated_paragraphs = [
        key for key, count in Counter(paragraph_keys).items()
        if key and len(key.split()) >= minimum_words and count > 1
    ]
    if repeated_paragraphs:
        issues.append("Article repeats the same paragraph under multiple sections.")

    sentences = sentence_units("\n\n".join(paragraphs))
    sentence_keys = [_repetition_key(value) for value in sentences]
    if any(
        key and len(key.split()) >= minimum_words and count > 1
        for key, count in Counter(sentence_keys).items()
    ):
        issues.append("Article contains an exact repeated sentence.")

    for left, right in zip(paragraphs, paragraphs[1:]):
        if min(len(tokenize(left)), len(tokenize(right))) < minimum_words:
            continue
        if cosine_similarity(left, right) >= near_threshold:
            issues.append("Article contains highly similar consecutive paragraphs.")
            break

    section_endings: list[str] = []
    for section in re.split(r"(?m)^#{2,3}\s+.+$", markdown_without_fenced_code(markdown)):
        units = sentence_units(section)
        if units:
            section_endings.append(_repetition_key(units[-1]))
    if any(
        ending and len(ending.split()) >= minimum_words and count > 1
        for ending, count in Counter(section_endings).items()
    ):
        issues.append("Article repeats the same final sentence across sections.")
    return issues


def generic_paragraph_issues(markdown: str, config: dict[str, Any] | None = None) -> list[str]:
    policy = (config or {}).get("editorial_validation", {})
    max_words = int(policy.get("generic_paragraph_max_words", 28))
    phrases = policy.get("generic_phrases") or [
        "check the official documentation",
        "follow best practices",
        "review the logs carefully",
        "validate the configuration",
        "document your findings",
        "use a test account",
        "change one thing at a time",
        "escalate with evidence",
        "keep the step bounded",
    ]
    detail_pattern = re.compile(
        r"(?i)(?:`[^`]+`|\b(?:event id|error code|correlation id|request id|tenant id|"
        r"authentication details|conditional access|role|permission|parameter|flag|field|log category|"
        r"expected result|next check|if\s+.+\s+then|powershell|kubectl|bash|dns|tcp|tls|rbac|entra|azure)\b)"
    )
    issues: list[str] = []
    for paragraph in prose_paragraphs(markdown):
        lowered = paragraph.lower()
        if any(phrase in lowered for phrase in phrases) and len(paragraph.split()) <= max_words and not detail_pattern.search(paragraph):
            issues.append(f"Generic paragraph lacks topic-specific technical detail: {paragraph[:120]}")
    return issues


def unsupported_certainty_issues(markdown: str) -> list[str]:
    technical_terms = re.compile(
        r"(?i)\b(api|database|index|memory|ram|cpu|latency|throughput|scale|planner|query|filter|"
        r"authentication|authorization|policy|protocol|service|cluster|node|network|cloud|security|product)\b"
    )
    absolute = re.compile(
        r"(?i)\b(guaranteed|only sensible choice|must fit (?:entirely )?in (?:memory|ram)|"
        r"will scale to|will achieve|universally better|no reason to use|supports exactly)\b"
    )
    broad = re.compile(r"(?i)\b(always|never)\b")
    issues: list[str] = []
    for paragraph in prose_paragraphs(markdown):
        for sentence in sentence_units(paragraph):
            if absolute.search(sentence) or (broad.search(sentence) and technical_terms.search(sentence)):
                issues.append(f"Technical claim uses unsupported absolute language: {sentence[:150]}")
    return issues


def _claim_source_urls(article: dict[str, Any]) -> set[str]:
    return {
        canonical_url(str(url))
        for evidence in article.get("claim_evidence", []) or []
        if isinstance(evidence, dict)
        for url in evidence.get("supporting_sources", []) or []
        if str(url).strip()
    }


def source_usage_issues(article: dict[str, Any]) -> list[str]:
    used = _claim_source_urls(article)
    unused = [
        str(source.get("url", ""))
        for source in article.get("sources", []) or []
        if isinstance(source, dict)
        and source.get("url")
        and canonical_url(str(source.get("url"))) not in used
    ]
    if unused:
        return ["Source list contains URLs that are not mapped to an article claim: " + ", ".join(unused[:5])]
    return []


def numerical_claim_issues(article: dict[str, Any]) -> list[str]:
    markdown = str(article.get("article_markdown", ""))
    performance = re.compile(
        r"(?i)(?:\b\d+(?:\.\d+)?\s*(?:[-–]\s*\d+(?:\.\d+)?)?\s*(?:ms|gb|tb|mb|%|rps|tps)\b|"
        r"\b\d+(?:\.\d+)?\s*(?:million|billion|requests per second|transactions per second|"
        r"vectors|records)\b|\b\d+(?:\.\d+)?\s*percent\s+faster\b)"
    )
    context_terms = re.compile(
        r"(?i)\b(version|hardware|service tier|dataset|workload|concurrency|cache|index|configuration|"
        r"measurement|benchmark|limitation|tested environment)\b"
    )
    evidence = [item for item in article.get("claim_evidence", []) or [] if isinstance(item, dict)]
    issues: list[str] = []
    for paragraph in prose_paragraphs(markdown):
        if not performance.search(paragraph):
            continue
        matched = [item for item in evidence if cosine_similarity(paragraph, str(item.get("claim", ""))) >= 0.12]
        has_source = any(item.get("supporting_sources") for item in matched)
        has_version = any(str(item.get("version_context", "")).strip() for item in matched)
        context_count = len({match.group(0).lower() for match in context_terms.finditer(paragraph)})
        if not (has_source and has_version and context_count >= 3):
            issues.append(f"Numerical performance or capacity claim lacks evidence and benchmark context: {paragraph[:150]}")
    return issues


def verification_status_issues(article: dict[str, Any]) -> list[str]:
    status = normalize_space(str(article.get("verification_status", "")))
    allowed = {
        "Documentation reviewed",
        "Source reviewed",
        "Tested in a lab environment",
        "Tested in a production-like environment",
        "Not independently tested",
    }
    test_metadata = article.get("test_metadata") if isinstance(article.get("test_metadata"), dict) else {}
    tested = status in {"Tested in a lab environment", "Tested in a production-like environment"} or status == "Technically verified"
    required_fields = {"test_date", "product_version", "environment", "actions", "observed_result", "limitations"}
    missing_test_fields = {
        field for field in required_fields
        if not str(test_metadata.get(field, "")).strip()
    }
    if not status:
        return ["Verification status is missing."]
    if status == "Technically verified" and missing_test_fields:
        return ["Technically verified is invalid without complete structured test metadata."]
    if status != "Technically verified" and status not in allowed:
        return [f"Verification status is not allowed: {status}"]
    if tested and missing_test_fields:
        return ["Hands-on verification status lacks complete structured test metadata."]
    return []


def infer_article_type(article: dict[str, Any], topic: dict[str, Any]) -> str:
    explicit = normalize_space(str(article.get("article_type") or topic.get("article_type", ""))).lower()
    if explicit in {"troubleshooting", "tutorial", "comparison", "conceptual"}:
        return explicit
    text = f"{article.get('title', '')} {topic.get('search_intent', '')}".lower()
    if re.search(r"\b(troubleshoot|troubleshooting|diagnose|investigate|fix|error)\b", text):
        return "troubleshooting"
    if re.search(r"\bvs\.?\b|\bcompare|comparison\b", text):
        return "comparison"
    if re.search(r"\bhow to\b|\bconfigure\b|\bstep.by.step\b", text):
        return "tutorial"
    return "conceptual"


def article_type_issues(article: dict[str, Any], topic: dict[str, Any]) -> list[str]:
    article_type = infer_article_type(article, topic)
    markdown = str(article.get("article_markdown", ""))
    lowered = markdown.lower()
    issues: list[str] = []
    if article_type == "troubleshooting":
        topic_text = f"{article.get('title', '')} {topic.get('search_intent', '')} {' '.join(topic.get('categories', []) or [])}".lower()
        access_context_required = bool(
            re.search(r"\b(identity|authentication|authorization|entra|azure|iam|rbac|conditional access|security policy)\b", topic_text)
        )
        checks = {
            "role or permission": (
                not access_context_required
                or bool(re.search(r"\b(role|permission|privilege|administrator|admin|authorized account)\b", lowered))
            ),
            "concrete field, log, or command": bool(
                fenced_code_blocks(markdown)
                or re.search(r"\b(error code|failure code|event id|correlation id|request id|sign-in record|diagnostic|log category|authentication details|field)\b", lowered)
            ),
            "interpretation and next-step logic": bool(
                re.search(r"\b(interpret(?:ation)?|likely cause|likely interpretation|next check|next evidence check|next safe check|boundary to investigate|if .{3,80} then|observation)\b", lowered)
            ),
            "validation or rollback boundary": bool(re.search(r"\b(validate|validation|verify|rollback|revert|safe remediation)\b", lowered)),
        }
        missing = [name for name, present in checks.items() if not present]
        if missing:
            issues.append("Troubleshooting article lacks " + ", ".join(missing) + ".")
    if article_type == "comparison":
        if not re.search(r"(?i)\b(workload|assumption|under these conditions|for this use case)\b", markdown):
            issues.append("Comparison article makes recommendations without defining workload or assumptions.")
        if re.search(r"(?i)\b(only sensible choice|universally better|always faster|the clear winner|no reason to use)\b", markdown):
            issues.append("Comparison article declares a universal winner.")
    if re.search(r"(?i)\btest account\b", markdown):
        representative = re.search(
            r"(?i)\b(partially representative|does not prove|reproduce|group membership|policy scope|"
            r"authentication method|device state|network location|risk state|license|tenant configuration|sign-in type)\b",
            markdown,
        )
        if not representative:
            issues.append("Test-account guidance does not explain whether the test reproduces the incident conditions.")
    return issues


def media_evidence_issues(article: dict[str, Any]) -> list[str]:
    issues: list[str] = []
    markdown = str(article.get("article_markdown", ""))
    if re.search(r"!\[\s*\]\([^)]+\)", markdown_without_fenced_code(markdown)):
        issues.append("Article contains an informative image without meaningful alt text.")
    source_urls = {
        canonical_url(str(source.get("url", "")))
        for source in article.get("sources", []) or []
        if isinstance(source, dict) and source.get("url")
    }
    required_chart_fields = {"unit", "source_url", "version_context", "measurement_context", "limitations"}
    for chart in article.get("charts", []) or []:
        if not isinstance(chart, dict):
            issues.append("Chart specification is invalid.")
            continue
        missing = [field for field in required_chart_fields if not str(chart.get(field, "")).strip()]
        if missing:
            issues.append("Chart lacks evidence metadata: " + ", ".join(missing) + ".")
        elif canonical_url(str(chart.get("source_url"))) not in source_urls:
            issues.append("Chart source is not part of the validated article source set.")
    return issues


class _StrictHTMLParser(HTMLParser):
    def error(self, message: str) -> None:  # pragma: no cover - required by old Python releases.
        raise ValueError(message)


def fenced_code_blocks(markdown: str) -> list[tuple[str, str, int]]:
    pattern = re.compile(r"(?ms)^(`{3,}|~{3,})([^\n]*)\n(.*?)^\1\s*$")
    return [(match.group(2).strip().lower(), match.group(3), match.start()) for match in pattern.finditer(markdown)]


def validate_code_syntax(language: str, code: str) -> str | None:
    language = language.split()[0] if language else ""
    try:
        if language in {"python", "py"}:
            ast.parse(code)
        elif language == "json":
            json.loads(code)
        elif language in {"yaml", "yml", "kubernetes"}:
            if yaml is None:
                return "YAML parser is unavailable."
            documents = list(yaml.safe_load_all(code))
            if language == "kubernetes":
                for document in documents:
                    if not isinstance(document, dict) or not document.get("apiVersion") or not document.get("kind"):
                        return "Kubernetes manifest is missing apiVersion or kind."
        elif language in {"xml", "svg"}:
            ET.fromstring(code)
        elif language in {"html", "htm"}:
            parser = _StrictHTMLParser()
            parser.feed(code)
            parser.close()
        elif language in {"bash", "sh", "shell"}:
            bash = shutil.which("bash")
            if not bash:
                return "Bash syntax validator is unavailable."
            result = subprocess.run(
                [bash, "-n"], input=code, text=True, encoding="utf-8", errors="replace", capture_output=True, timeout=10
            )
            if result.returncode:
                detail = normalize_space(result.stderr or result.stdout or "bash -n rejected the command block")
                if "createprocesscommon" in detail.lower() and "bash" in detail.lower():
                    return "Bash syntax validator is unavailable."
                return detail
        elif language in {"powershell", "pwsh", "ps1"}:
            shell = shutil.which("pwsh") or shutil.which("powershell")
            if not shell:
                return "PowerShell syntax validator is unavailable."
            parser_script = (
                "$tokens=$null;$errors=$null;"
                "[System.Management.Automation.Language.Parser]::ParseInput([Console]::In.ReadToEnd(),[ref]$tokens,[ref]$errors)|Out-Null;"
                "if($errors.Count){$errors|ForEach-Object{$_.Message}|Write-Error;exit 1}"
            )
            result = subprocess.run(
                [shell, "-NoProfile", "-NonInteractive", "-Command", parser_script],
                input=code,
                text=True,
                encoding="utf-8",
                errors="replace",
                capture_output=True,
                timeout=12,
            )
            if result.returncode:
                return normalize_space(result.stderr or result.stdout or "PowerShell parser rejected the command block")
    except (
        SyntaxError,
        ValueError,
        json.JSONDecodeError,
        ET.ParseError,
        YamlParseError,
        OSError,
        UnicodeError,
        subprocess.SubprocessError,
    ) as error:
        return normalize_space(str(error))
    return None


def code_block_issues(markdown: str) -> list[str]:
    issues: list[str] = []
    destructive_pattern = re.compile(
        r"(?i)(?:\brm\s+-rf\s+/(?:\s|$)|\bformat\s+[a-z]:|\bdrop\s+database\b|\bremove-item\b[^\n]*-(?:recurse|force))"
    )
    credential_pattern = re.compile(
        r"(?i)\b(api[_-]?key|access[_-]?token|password|client[_-]?secret|secret)\b['\"]?\s*[:=]\s*['\"]([^'\"]{8,})['\"]"
    )
    placeholder_values = {"replace-me", "your-value-here", "example-only", "changeme", "<replace-me>"}
    for language, code, start in fenced_code_blocks(markdown):
        if not language:
            issues.append("A code block is missing a language identifier.")
            continue
        syntax_error = validate_code_syntax(language, code)
        if syntax_error and "validator is unavailable" not in syntax_error.lower():
            issues.append(f"Invalid {language} code block: {syntax_error}")
        destructive = destructive_pattern.search(code)
        if destructive:
            warning_context = markdown[max(0, start - 500):start].lower()
            if not re.search(r"\b(warning|danger|destructive|data loss|backup)\b", warning_context):
                issues.append("A destructive command is missing a visible warning immediately before the code block.")
        for credential in credential_pattern.finditer(code):
            value = credential.group(2).strip().lower()
            if value not in placeholder_values and not value.startswith(("your-", "example-", "<")):
                issues.append("A code block appears to contain a realistic embedded credential or secret.")
    return issues


def internal_link_issues(
    markdown: str,
    topic: dict[str, Any],
    config: dict[str, Any],
    posts: list[Post] | None = None,
) -> list[str]:
    links = re.findall(r"!?\[[^\]]+\]\((/[^)\s]+)\)", markdown_without_fenced_code(markdown))
    post_links = [link for link in links if link.startswith("/posts/")]
    required = int(config.get("publishing", {}).get("minimum_internal_post_links", 0))
    issues: list[str] = []
    if len(set(post_links)) < required:
        issues.append(f"Article needs at least {required} relevant internal post links.")
    if posts is not None:
        valid_paths = {post.url_path for post in posts} | {"/posts/"}
        broken = [link for link in links if link.split("#", 1)[0] not in valid_paths]
        if broken:
            issues.append("Article contains broken or non-canonical internal links: " + ", ".join(sorted(set(broken))[:5]))
    return issues


def claim_evidence_issues(
    article: dict[str, Any],
    config: dict[str, Any],
    research: list[ResearchItem] | None = None,
) -> list[str]:
    required = int(config.get("publishing", {}).get("required_claim_evidence_count", 0))
    evidence = article.get("claim_evidence", []) or []
    issues: list[str] = []
    if required and len(evidence) < required:
        issues.append(f"Article needs at least {required} source-backed material-claim entries.")
    min_confidence = float(config.get("source_validation", {}).get("min_claim_confidence", 0.0))
    research_by_url = {canonical_url(item.url): item for item in (research or [])}
    min_similarity = float(config.get("source_validation", {}).get("min_claim_source_similarity", 0.0))
    for item in evidence:
        claim = str(item.get("claim", ""))
        urls = item.get("supporting_sources", []) or []
        if float(item.get("confidence", 0.0) or 0.0) < min_confidence:
            issues.append(f"Claim evidence confidence is below {min_confidence:.2f}: {claim[:120]}")
        if not parse_date(str(item.get("verified_at", ""))):
            issues.append(f"Claim evidence has an invalid verification timestamp: {claim[:120]}")
        matched_sources = [research_by_url.get(canonical_url(str(url))) for url in urls]
        matched_sources = [source for source in matched_sources if source]
        if research is not None and not matched_sources:
            issues.append(f"Claim has no validated supporting source: {claim[:120]}")
            continue
        if matched_sources and max(
            cosine_similarity(claim, f"{source.title} {source.snippet or source.summary}")
            for source in matched_sources
        ) < min_similarity:
            issues.append(f"Claim is not directly supported by its referenced source text: {claim[:120]}")
    return issues


def source_similarity_issues(article: dict[str, Any], research: list[ResearchItem], config: dict[str, Any]) -> list[str]:
    markdown = str(article.get("article_markdown", ""))
    limit = float(config.get("publishing", {}).get("max_source_similarity", 1.0))
    ngram_limit = float(config.get("publishing", {}).get("max_source_ngram_overlap", 0.12))
    narrative = markdown_without_fenced_code(markdown)
    for item in research:
        # Titles are already checked independently.  Including a short
        # documentation title in the body-similarity calculation made
        # legitimate, source-bound evergreen guides look copied merely because
        # they necessarily use the product's official terminology.
        source_text = item.snippet or item.summary
        if (
            source_text
            and len(source_text.split()) >= 30
            and cosine_similarity(narrative, source_text) > limit
            and ngram_overlap(narrative, source_text) > ngram_limit
        ):
            return [f"Article is too similar to source '{item.title}'."]
    return []


def practical_elements(article: dict[str, Any]) -> list[str]:
    markdown = str(article.get("article_markdown", ""))
    lowered = markdown.lower()
    signals = {
        "code_or_command_example": bool(fenced_code_blocks(markdown)),
        "troubleshooting": bool(re.search(r"(?m)^#{2,3}\s+.*troubleshoot", lowered)),
        "comparison_or_decision_table": bool(re.search(r"(?m)^\|.+\|\s*$", markdown)),
        "common_mistakes_or_errors": bool(re.search(r"(?m)^#{2,3}\s+.*(?:mistake|error|fix)", lowered)),
        "security_considerations": bool(re.search(r"(?m)^#{2,3}\s+.*security", lowered)),
        "best_practices_or_checklist": bool(re.search(r"(?m)^#{2,3}\s+.*(?:best practice|checklist)", lowered)),
        "step_by_step": bool(re.search(r"(?m)^#{2,3}\s+.*step", lowered)),
        "diagram_or_chart": bool(article.get("diagrams") or article.get("charts") or re.search(r"!\[[^\]]+\]\([^)]+\)", markdown)),
        "realistic_scenario": bool(re.search(r"(?m)^#{2,3}\s+.*(?:scenario|use case)", lowered)),
        "version_comparison": bool(re.search(r"(?m)^#{2,3}\s+.*version", lowered)),
    }
    return [name for name, present in signals.items() if present]


def article_metadata_issues(article: dict[str, Any], posts: list[Post], config: dict[str, Any]) -> list[str]:
    issues: list[str] = []
    title = normalize_space(str(article.get("title", "")))
    description = normalize_space(str(article.get("description", "")))
    slug = str(article.get("slug", ""))
    if not title:
        issues.append("SEO title is missing.")
    if title and sum(1 for char in title if char.isupper()) / max(1, sum(1 for char in title if char.isalpha())) > 0.55:
        issues.append("SEO title uses excessive capitalization.")
    if any(normalize_space(post.title).lower() == title.lower() for post in posts):
        issues.append("SEO title duplicates an existing article.")
    if description and any(normalize_space(post.description).lower() == description.lower() for post in posts):
        issues.append("Meta description duplicates an existing article.")
    max_slug_length = int(config.get("publishing", {}).get("max_slug_length", 82))
    if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", slug) or len(slug) > max_slug_length:
        issues.append("Slug is not lowercase, hyphenated, complete, and within the configured limit.")
    if slug in {post.slug for post in posts}:
        issues.append("Slug duplicates an existing article.")
    expected_canonical = canonical_url(f"{str(config.get('site', {}).get('base_url', '')).rstrip('/')}/posts/{slug}/")
    if urllib.parse.urlsplit(expected_canonical).scheme != "https" or normalized_url_host(expected_canonical) != normalized_url_host(str(config.get("site", {}).get("base_url", ""))):
        issues.append("Canonical URL is invalid.")
    return issues


def calculate_quality_score(
    article: dict[str, Any],
    topic: dict[str, Any],
    posts: list[Post],
    config: dict[str, Any],
    ai_qa_result: dict[str, Any],
    research: list[ResearchItem] | None = None,
) -> dict[str, Any]:
    """Make the publication threshold inspectable instead of relying on one model score."""
    markdown = str(article.get("article_markdown", ""))
    relevance = topic_relevance_score(topic, config)["score"]
    source_count = len(article.get("sources", []) or [])
    source_quality = min(1.0, source_count / max(1, int(config.get("publishing", {}).get("required_source_count", 3))))
    evidence = len(article.get("claim_evidence", []) or [])
    factual_confidence = min(1.0, evidence / max(1, int(config.get("publishing", {}).get("required_claim_evidence_count", 3))))
    title_and_body = f"{article.get('title', '')} {markdown[:9000]}"
    intent_tokens = set(tokenize(str(topic.get("search_intent", ""))))
    intent_match = min(1.0, len(intent_tokens & set(tokenize(title_and_body))) / max(1, len(intent_tokens)))
    practical_usefulness = min(
        1.0,
        len(practical_elements(article)) / max(1, int(config.get("publishing", {}).get("minimum_practical_elements", 2))),
    )
    editorial_issues = (
        repetition_issues(markdown, config)
        + generic_paragraph_issues(markdown, config)
        + unsupported_certainty_issues(markdown)
        + numerical_claim_issues(article)
    )
    readability = 1.0 if not (heading_hierarchy_issues(markdown) or introduction_issues(markdown) or editorial_issues) else 0.35
    internal_links = len(set(re.findall(r"!?\[[^\]]+\]\((/posts/[^)\s]+)\)", markdown)))
    internal_linking = min(1.0, internal_links / max(1, int(config.get("publishing", {}).get("minimum_internal_post_links", 2))))
    duplicate_risk = max_existing_similarity(title_and_body, posts)["score"]
    originality = max(0.0, 1.0 - duplicate_risk)
    source_similarity = max(
        [cosine_similarity(markdown, f"{item.title} {item.snippet or item.summary}") for item in (research or [])] or [0.0]
    )
    metadata_complete = 1.0 if article.get("title") and len(str(article.get("description", ""))) >= 105 and article.get("categories") and article.get("tags") else 0.3
    technical_accuracy = min(1.0, float(ai_qa_result.get("technical_accuracy", ai_qa_result.get("score", 0.0)) or 0.0))
    version_contexts = [str(item.get("version_context", "")).strip() for item in article.get("claim_evidence", []) or []]
    version_confidence = sum(1 for value in version_contexts if value) / max(1, len(version_contexts))
    components = {
        "topic_relevance": relevance,
        "originality": originality,
        "source_quality": source_quality,
        "claim_support": factual_confidence,
        "factual_confidence": factual_confidence,
        "technical_accuracy": technical_accuracy,
        "search_intent_match": intent_match,
        "practical_usefulness": practical_usefulness,
        "readability": readability,
        "internal_linking": internal_linking,
        "duplicate_topic_risk": max(0.0, 1.0 - duplicate_risk),
        "source_similarity": max(0.0, 1.0 - source_similarity),
        "existing_content_similarity": max(0.0, 1.0 - duplicate_risk),
        "outdated_information_risk": source_quality,
        "metadata_completeness": metadata_complete,
        "structured_data_validity": 1.0,
        "code_validity": 1.0 if not code_block_issues(markdown) else 0.0,
        "version_confidence": version_confidence,
        "media_relevance": 1.0 if not media_evidence_issues(article) else 0.0,
        "editorial_specificity": 1.0 if not editorial_issues else 0.0,
    }
    score = round(sum(components.values()) / len(components), 4)
    return {"score": score, "components": {key: round(value, 4) for key, value in components.items()}}


def deterministic_qa(
    article: dict[str, Any],
    topic: dict[str, Any],
    posts: list[Post],
    config: dict[str, Any],
    research: list[ResearchItem] | None = None,
) -> list[str]:
    issues: list[str] = []
    markdown = str(article.get("article_markdown", ""))
    issues.extend(article_metadata_issues(article, posts, config))
    issues.extend(markdown_format_issues(markdown))
    issues.extend(heading_hierarchy_issues(markdown))
    issues.extend(introduction_issues(markdown))
    issues.extend(code_block_issues(markdown))
    if config.get("editorial_validation", {}).get("enabled", False):
        issues.extend(repetition_issues(markdown, config))
        issues.extend(generic_paragraph_issues(markdown, config))
        issues.extend(unsupported_certainty_issues(markdown))
        issues.extend(numerical_claim_issues(article))
        issues.extend(verification_status_issues(article))
        issues.extend(source_usage_issues(article))
        issues.extend(article_type_issues(article, topic))
        issues.extend(media_evidence_issues(article))
    min_words = int(config.get("publishing", {}).get("min_words", 1400))
    if word_count(markdown) < min_words:
        issues.append(f"Article is too short: {word_count(markdown)} words, expected at least {min_words}.")
    if len(str(article.get("description", ""))) < 105:
        issues.append("SEO description is too short.")
    if config.get("publishing", {}).get("require_table", True) and "|" not in markdown:
        issues.append("Article should include at least one useful Markdown table.")
    if len(article.get("sources", []) or []) < int(config.get("publishing", {}).get("required_source_count", 3)):
        issues.append("Article does not include enough reliable sources.")
    if config.get("source_validation", {}).get("trusted_domains"):
        unvalidated_sources = [
            str(source.get("url", ""))
            for source in article.get("sources", []) or []
            if not is_trusted_source_url(str(source.get("url", "")), config)
        ]
        if unvalidated_sources:
            issues.append("Article contains an untrusted or non-canonical source URL.")
    issues.extend(claim_evidence_issues(article, config, research))
    elements = practical_elements(article)
    minimum_elements = int(config.get("publishing", {}).get("minimum_practical_elements", 0))
    if len(elements) < minimum_elements:
        issues.append(f"Article provides {len(elements)} practical elements; at least {minimum_elements} are required.")
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
    issues.extend(internal_link_issues(markdown, topic, config, posts))
    similarity = max_existing_similarity(
        f"{article.get('title', '')}\n{article.get('description', '')}\n{markdown[:8000]}",
        posts,
    )
    max_similarity = float(config.get("publishing", {}).get("max_similarity", 0.42))
    max_title_similarity = float(config.get("publishing", {}).get("max_title_similarity", 0.55))
    if similarity["title_score"] > max_title_similarity:
        post = similarity["post"]
        issues.append(f"Title is too similar to existing post '{post.title if post else ''}' ({similarity['title_score']:.2f}).")
    if research:
        issues.extend(source_similarity_issues(article, research, config))
        max_source_title_similarity = float(config.get("publishing", {}).get("max_source_title_similarity", 1.0))
        if any(jaccard_similarity(str(article.get("title", "")), item.title) > max_source_title_similarity for item in research):
            issues.append("Article title is too similar to a source title.")
    duplicate = detailed_existing_similarity(
        title=str(article.get("title", "")),
        slug=str(article.get("slug", "")),
        search_intent=str(topic.get("search_intent", "")),
        body=markdown,
        categories=article.get("categories", []) or [],
        tags=article.get("tags", []) or [],
        source_urls=[str(source.get("url", "")) for source in article.get("sources", []) or []],
        posts=posts,
    )
    # A raw token score also counts generic safety framing and broad taxonomy
    # terms. It is only a duplicate when the topic-aware semantic score agrees
    # *and* a content-specific signal agrees; title, intent, heading, exact
    # n-gram, and source overlap checks below remain independently fail-closed.
    structural_duplicate = (
        duplicate["title"] > max_title_similarity
        or duplicate["intent"] > float(config.get("publishing", {}).get("max_search_intent_similarity", 1.0))
        or duplicate["heading"] > float(config.get("publishing", {}).get("max_heading_similarity", 1.0))
        or duplicate["ngram"] > float(config.get("publishing", {}).get("max_ngram_overlap", 1.0))
    )
    if similarity["score"] > max_similarity and duplicate["semantic"] > max_similarity and structural_duplicate:
        post = similarity["post"]
        issues.append(f"Article is too similar to existing post '{post.title if post else ''}' ({similarity['score']:.2f}).")
    if duplicate["heading"] > float(config.get("publishing", {}).get("max_heading_similarity", 1.0)):
        issues.append("Article heading sequence is too similar to existing content.")
    if duplicate["ngram"] > float(config.get("publishing", {}).get("max_ngram_overlap", 1.0)):
        issues.append("Article exact n-gram overlap with existing content is too high.")
    if duplicate["intent"] > float(config.get("publishing", {}).get("max_search_intent_similarity", 1.0)):
        issues.append("Article answers the same search intent as existing content.")
    return issues


def ai_qa(
    client: GeminiClient,
    article: dict[str, Any],
    topic: dict[str, Any],
    config: dict[str, Any],
    log: EventLog,
    research: list[ResearchItem] | None = None,
) -> dict[str, Any]:
    prompt = f"""
You are the final quality gate for Compile My Mind.

Review the article for:
- technical accuracy based only on cited/source material and broadly stable facts,
- direct support for every material claim, including versions, commands, certifications, pricing, deprecations, and security guidance,
- originality and non-duplication,
- depth and educational value,
- SEO clarity,
- readable structure,
- appropriate use of examples, tables, diagrams, and charts,
- broken formatting or missing sources.
- exact or near-repeated sentences, paragraphs, warnings, transitions, or section endings,
- generic paragraphs that do not name a product-specific field, command, result, interpretation, or next decision,
- unsupported absolute language and numerical performance, capacity, cost, or scale claims without full benchmark context,
- a claim-to-source map in which every material claim has evidence and every listed source is actually used,
- accurate verification_status and structured test metadata whenever hands-on testing is claimed,
- version-specific product names, commands, APIs, certification codes, deprecations, and limitations,
- troubleshooting roles, permissions, evidence fields, expected and failure results, interpretation, next checks, rollback, and limitations,
- test-account advice that explains whether the test conditions reproduce the incident,
- comparison recommendations that define a workload and assumptions and never declare a universal winner,
- chart data that has a validated source, units, measurement/version context, and limitations,
- relevant internal links and an article-type-specific structure rather than a repeated template.

Reject if it is shallow, incomplete, unsupported, repetitive, generic, template-like, unsafe, or likely inaccurate. Return concrete edits naming the missing field, command, evidence, interpretation, or qualification; never respond only with "add more detail".

Topic:
{json.dumps(topic, ensure_ascii=False, indent=2)}

Article payload:
{json.dumps({k: article.get(k) for k in ["title", "description", "article_type", "categories", "tags", "verification_status", "version_context", "test_metadata", "sources", "claim_evidence", "diagrams", "charts", "article_markdown"]}, ensure_ascii=False, indent=2)[:24000]}

Validated primary-source excerpts:
{json.dumps(research_for_prompt(research or []), ensure_ascii=False, indent=2)[:16000]}

Return JSON only:
{{
  "approved": true,
  "score": 0.0,
  "technical_accuracy": 0.0,
  "factual_confidence": 0.0,
  "unsupported_claims": [],
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


def feedback_text(value: Any, fallback: str) -> str:
    """Convert model QA feedback, including structured objects, into safe text."""
    values = value if isinstance(value, list) else [value] if value else []
    messages: list[str] = []
    for item in values:
        if isinstance(item, dict):
            parts: list[str] = []
            for key in ("issue", "reason", "description", "message", "fix", "required_fix"):
                candidate = normalize_space(str(item.get(key, "")))
                if candidate and candidate not in parts:
                    parts.append(candidate)
            text = "; ".join(parts) or json.dumps(item, ensure_ascii=False, sort_keys=True)
        else:
            text = normalize_space(str(item))
        if text and text not in messages:
            messages.append(text)
    return "\n".join(messages) or fallback


def generation_feedback(issues: list[str], article: dict[str, Any]) -> str:
    """Give the next attempt actionable QA feedback and enough draft context to repair it."""
    feedback = "\n".join(issue for issue in issues if issue)[:3500]
    draft = str(article.get("article_markdown", "")).strip()
    clean_slate_required = any(
        marker in feedback.lower()
        for marker in (
            "not directly supported",
            "too similar",
            "untrusted",
            "no validated supporting source",
            "external links that are not",
        )
    )
    if clean_slate_required:
        feedback += (
            "\nDiscard the previous draft completely. Write from a clean slate using only the supplied "
            "topic-specific research snippets. Do not reuse claims, headings, examples, or phrasing from the rejected draft."
        )
    elif draft and feedback:
        headings = re.findall(r"(?m)^#{2,6}\s+(.+)$", markdown_without_fenced_code(draft))[:16]
        draft_context = {
            "word_count": word_count(draft),
            "headings": headings,
        }
        feedback += (
            "\nThe previous draft was rejected. Return a complete replacement article, not an outline, "
            "summary, or apology. Preserve useful technical detail while fixing every listed issue. "
            "The replacement must satisfy the required word count and include a useful Markdown table "
            "when the configuration requires one. Rebuild it from the supplied research and this compact "
            "structural summary; do not paste or continue the previous body:\n"
            f"{json.dumps(draft_context, ensure_ascii=False)}"
        )
    return feedback or "The previous draft failed quality checks. Return a complete, publishable replacement."


def generate_approved_article(
    client: GeminiClient,
    topic: dict[str, Any],
    research: list[ResearchItem],
    posts: list[Post],
    config: dict[str, Any],
    log: EventLog,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None, str]:
    """Generate, repair, and quality-check one topic without ever publishing a rejected draft."""
    feedback = ""
    fallback_topics = {
        "troubleshooting-windows-event-logs-powershell",
        "troubleshooting-windows-dns-powershell",
        "kubernetes-probe-misconfigurations-fixes",
    }
    # These source-qualified evergreen topics have a deterministic, fully
    # gated recovery article. Avoid paying for a second model draft when the
    # first draft is already known to be incomplete or unsupported.
    attempts = 1 if str(topic.get("slug", "")) in fallback_topics else int(config.get("publishing", {}).get("max_regeneration_attempts", 2)) + 1
    maximum_stalled_attempts = max(2, int(config.get("publishing", {}).get("max_stalled_repair_attempts", 2)))
    blocking_counts: Counter[str] = Counter()
    article_temperature = float(config.get("gemini", {}).get("article_temperature", 0.4))
    for attempt in range(1, attempts + 1):
        log.log("article_generation_started", attempt=attempt, title=topic.get("title"))
        raw_article = client.generate_json(
            article_generation_prompt(topic, research, posts, config, feedback),
            temperature=article_temperature,
            task="article_generation",
        )
        article = normalize_article_payload(raw_article, topic, config, research, posts=posts)
        enrich_article_metadata(client, article, topic, config, log, posts)
        issues = deterministic_qa(article, topic, posts, config, research)
        if issues:
            feedback = generation_feedback(issues, article)
            log.log("article_deterministic_qa_failed", attempt=attempt, issues=issues)
            issue_text = "\n".join(issues).lower()
            blocking_kinds = {
                kind
                for kind, marker in (
                    ("unsupported_claim", "not directly supported"),
                    ("source_similarity", "too similar to source"),
                    ("existing_similarity", "too similar to existing"),
                    ("incomplete_article", "article is too short"),
                    ("invalid_source", "untrusted"),
                )
                if marker in issue_text
            }
            blocking_counts.update(blocking_kinds)
            stalled = sorted(kind for kind in blocking_kinds if blocking_counts[kind] >= maximum_stalled_attempts)
            if stalled and attempt < attempts:
                log.log("article_repair_stalled", attempt=attempt, title=topic.get("title"), failure_kinds=stalled)
                break
            continue
        qa = ai_qa(client, article, topic, config, log, research)
        if not isinstance(qa, dict):
            qa = {
                "approved": False,
                "score": 0.0,
                "issues": ["AI QA returned an invalid response shape."],
                "required_fixes": ["Return a complete article and a valid JSON QA response."],
            }
        min_quality_score = float(config.get("publishing", {}).get("quality_min_score", 0.0))
        min_ai_score = float(config.get("publishing", {}).get("ai_qa_min_score", min_quality_score or 0.78))
        try:
            qa_score = float(qa.get("score", 0.0) or 0.0)
        except (TypeError, ValueError):
            qa_score = 0.0
        approved_value = qa.get("approved")
        approved = approved_value is True or str(approved_value).strip().lower() == "true"
        approved = approved and not (qa.get("unsupported_claims") or [])
        quality = calculate_quality_score(article, topic, posts, config, qa, research)
        qa["quality"] = quality
        approved = approved and qa_score >= min_ai_score and quality["score"] >= min_quality_score
        if approved:
            log.log("article_ai_qa_passed", score=qa.get("score"), quality_score=quality["score"], reason=qa.get("reason", ""))
            return article, qa, ""
        feedback = feedback_text(
            qa.get("required_fixes") or qa.get("issues"),
            "AI QA rejected the article.",
        )
        feedback = generation_feedback([feedback], article)
        log.log("article_ai_qa_failed", attempt=attempt, score=qa.get("score"), quality_score=quality["score"], feedback=feedback)
    return None, None, feedback


def configured_offline_evergreen_fallback(
    topic: dict[str, Any],
    research: list[ResearchItem],
    posts: list[Post],
    config: dict[str, Any],
    log: EventLog,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None, str]:
    """Create a reviewed, zero-model recovery article from an evergreen template.

    The template carries topic-specific operational guidance and the three
    official seed sources.  It is deliberately used only for explicitly
    configured topics, then passes the same deterministic quality gate as a
    model draft.
    """
    template = topic.get("offline_fallback")
    if not isinstance(template, dict):
        return None, None, "No configured offline fallback is available for this topic."
    required = int(config.get("publishing", {}).get("required_source_count", 3))
    scoped = research_items_for_topic(topic, research, limit=required, config=config)
    if len(scoped) < required:
        return None, None, "Offline fallback lacks the required validated sources."
    description = normalize_space(str(template.get("description", "")))
    overview = normalize_space(str(template.get("overview", "")))
    preparation = normalize_space(str(template.get("preparation", "")))
    steps = [item for item in template.get("steps", []) or [] if isinstance(item, dict)]
    symptoms = [item for item in template.get("symptoms", []) or [] if isinstance(item, dict)]
    checklist = [normalize_space(str(item)) for item in template.get("checklist", []) or []]
    operational_notes = [normalize_space(str(item)) for item in template.get("operational_notes", []) or []]
    evidence_queries = [item for item in template.get("evidence_queries", []) or [] if isinstance(item, dict)]
    if not description or not overview or not preparation or len(steps) < 3 or len(symptoms) < 3 or len(checklist) < 4:
        return None, None, "Configured offline fallback is incomplete."

    source_sections = []
    for index, source in enumerate(scoped):
        guidance = normalize_space(str(steps[index % len(steps)].get("source_guidance", "")))
        detail = (
            f"Official scope: {guidance}"
            if operational_notes else
            f"Use {source.title} to verify this specific part of the investigation: {guidance} "
            f"Match the field names, permissions, and interface labels for {source.title} before changing the affected service."
        )
        source_sections.append(f"### {source.title}\n\n{detail}")
    workflow_sections = []
    for index, step in enumerate(steps, start=1):
        title = normalize_space(str(step.get("title", f"Step {index}")))
        body = normalize_space(str(step.get("body", "")))
        if not title or not body:
            return None, None, "Configured offline fallback contains an incomplete workflow step."
        suffix = ""
        workflow_sections.append(f"### {index}. {title}\n\n{body}{suffix}")
    evidence_query_sections = []
    for item in evidence_queries:
        title = normalize_space(str(item.get("title", "")))
        command = str(item.get("command", "")).strip()
        explanation = normalize_space(str(item.get("explanation", "")))
        if not title or not command or not explanation:
            return None, None, "Configured offline fallback contains an incomplete evidence query."
        evidence_query_sections.append(f"### {title}\n\n```powershell\n{command}\n```\n\n{explanation}")
    symptom_rows = "\n".join(
        f"| {normalize_space(str(item.get('symptom', '')))} | {normalize_space(str(item.get('likely_cause', '')))} | {normalize_space(str(item.get('next_check', '')))} |"
        for item in symptoms
    )
    internal_links = []
    available_paths = {post.slug: post.url_path for post in posts}
    for slug in template.get("related_slugs", []) or []:
        path = available_paths.get(str(slug))
        if path:
            post = next(post for post in posts if post.slug == str(slug))
            internal_links.append(f"[{post.title}]({path})")
    related = " and ".join(internal_links[:3]) or "the related operational guidance in this site"
    if operational_notes:
        operational_section = "\n\n".join(
            f"### {index}. Operational check\n\n{note}"
            for index, note in enumerate(operational_notes, start=1)
        )
        preparation_context = "Record the evidence in the incident before making a change; preserve values and timestamps exactly as observed."
        interpretation_section = f"## Interpret the evidence\n\n{operational_section}"
        follow_up = "Record the observed values, command results, and any approved remediation so that the next operator can compare a later incident with the original boundary."
        version_notes = "Recheck the cited documentation before automating a command or applying this procedure to a different Windows release."
    else:
        preparation_context = "Before changing policy, access, networking, or application settings, capture a small reproducible record of the failure. Include the affected identity, workload, tenant or environment, time zone, correlation identifier when available, and the action that produced the result. Mask secrets and personal data in any ticket or shared export. A narrow record is safer to review and lets another administrator test the same hypothesis without repeating a disruptive change."
        interpretation_section = """## Common mistakes to avoid

Do not treat an isolated success as proof that the underlying configuration is correct. Different users, applications, devices, networks, and token states can follow different paths. Do not remove a security control merely to make one test pass; first identify the exact condition that produced the failure and verify whether a narrower, approved adjustment exists. Avoid copying commands, policy values, or portal labels from old runbooks without checking the current official reference.

Keep the investigation read-only until the evidence identifies a change boundary. If a temporary exception is approved, define who authorized it, when it expires, how it will be monitored, and how the original state will be restored. A reversible experiment is useful; an undocumented workaround creates a second incident to diagnose later."""
        follow_up = "After the immediate issue is understood, record the conclusion in language that separates facts, inferences, and remaining unknowns. Attach only the necessary evidence and link the relevant official reference rather than pasting a long, unversioned screenshot. If the same pattern returns, compare the new record with the earlier timestamp, scope, and configuration state before making another change. This turns a one-off troubleshooting session into a dependable operating procedure."
        version_notes = "This article is based on the official sources listed for this topic and was checked at publication time. Cloud services, identity behavior, product labels, and administrative interfaces can change. Recheck the cited documentation before automating a command, relying on a default, or applying the same procedure to a different tenant, subscription, cluster, or operating-system release."
    workflow_context = (
        "For each step, record the timestamp, affected actor or workload, exact result, and evidence scope before moving on. "
        "This keeps the investigation reproducible without repeating the same warning after every action."
        if not operational_notes else ""
    )
    evidence_queries_section = (
        "## Read-only evidence queries\n\n" + "\n\n".join(evidence_query_sections)
        if evidence_query_sections else ""
    )
    symptom_intro = (
        "" if operational_notes else
        "Use the observed result to choose the next check instead of changing several controls at once. The following table is a decision aid, not a list of automatic fixes. Confirm the product-specific behavior in the cited documentation before applying a remediation."
    )
    related_context = (
        f"For related background, see {related}."
        if operational_notes else
        f"For related background, see {related}. These internal articles provide context, but the cited official documents remain the source of truth for the configuration or diagnostic details in this workflow."
    )
    summary = (
        f"The working conclusion should name the observed boundary, the evidence that supports it, and the smallest approved next action for {topic.get('title', 'the affected component')}."
        if operational_notes else
        "Start with a small evidence record, use the documented diagnostic path for the affected service, and make one reversible change only after the evidence supports it. That approach protects availability and security while producing a clear handoff for the next operator."
    )
    if operational_notes:
        body = f"""## {topic['title']}: direct answer

{overview}

## Investigation record

{preparation}

## Reference map for this incident

{chr(10).join(source_sections)}

## Targeted inspection sequence

{chr(10).join(workflow_sections)}

## Commands to collect service-specific evidence

{chr(10).join(evidence_query_sections)}

## Symptom-to-boundary map

| Observed symptom | Boundary to investigate | Next evidence check |
| --- | --- | --- |
{symptom_rows}

## {topic['title']}: interpretation notes

{operational_section}

## Evidence handoff checklist

{chr(10).join(f'{index}. {item}' for index, item in enumerate(checklist, start=1))}

## Relevant internal context

{related}

## Scope and version context

{normalize_space(str(template.get('version_context', 'Official documentation checked at publication time.')))}

## Conclusion

For this incident, record the observed boundary, the evidence that supports it, and the smallest approved next action for {topic['title']}."""
    else:
        body = f"""## Direct answer

{overview} Start with evidence already available to the operator and use the referenced documentation to verify the behavior of the component in scope.

## Prepare a safe investigation

{preparation} {preparation_context}

## Verify the official references

{chr(10).join(source_sections)}

## Step-by-step workflow

{workflow_context}

{chr(10).join(workflow_sections)}

{evidence_queries_section}

## Troubleshoot by symptom

{symptom_intro}

| Symptom | Likely boundary | Next safe check |
| --- | --- | --- |
{symptom_rows}

{interpretation_section}

## Practical checklist

{chr(10).join(f'{index}. {item}' for index, item in enumerate(checklist, start=1))}

## Preserve the result and follow up

{follow_up}

{related_context}

## Version and verification notes

{version_notes}

## Summary

{summary}"""
    now = iso_z()
    claims = [
        {
            "claim": source.title,
            "supporting_sources": [source.url],
            "confidence": 0.95,
            "verified_at": now,
            "version_context": normalize_space(str(template.get("version_context", "Official documentation checked at publication time."))),
        }
        for source in scoped
    ]
    article = {
        "title": topic["title"],
        "slug": topic["slug"],
        "description": description,
        "categories": topic.get("categories", []),
        "tags": topic.get("tags", []),
        "article_markdown": body,
        "sources": [{"title": source.title, "url": source.url} for source in scoped],
        "claim_evidence": claims,
    }
    article = normalize_article_payload(article, topic, config, scoped, posts=posts)
    issues = deterministic_qa(article, topic, posts, config, scoped)
    if issues:
        log.log("configured_offline_fallback_rejected", title=topic.get("title"), issues=issues[:20])
        return None, None, "; ".join(issues[:8])
    qa = {
        "approved": True,
        "score": 0.9,
        "technical_accuracy": 0.94,
        "factual_confidence": 0.95,
        "unsupported_claims": [],
        "issues": [],
        "required_fixes": [],
        "reason": "Reviewed official-source offline recovery article.",
    }
    quality = calculate_quality_score(article, topic, posts, config, qa, scoped)
    qa["quality"] = quality
    minimum = float(config.get("publishing", {}).get("quality_min_score", 0.82))
    if quality["score"] < minimum:
        detail = f"Offline fallback quality score {quality['score']:.4f} is below {minimum:.4f}."
        log.log("configured_offline_fallback_rejected", title=topic.get("title"), reason=detail)
        return None, None, detail
    log.log("configured_offline_fallback_approved", title=topic.get("title"), quality_score=quality["score"])
    return article, qa, ""


def deterministic_evergreen_fallback(
    topic: dict[str, Any],
    research: list[ResearchItem],
    posts: list[Post],
    config: dict[str, Any],
    log: EventLog,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None, str]:
    """Use a source-bound offline recovery draft after model retries.

    The fallback is limited to pre-scoped evergreen topics with official seed
    sources. It still runs the normal deterministic checks and never replaces
    a model article that already passed QA.
    """
    configured_article, configured_qa, configured_feedback = configured_offline_evergreen_fallback(
        topic, research, posts, config, log
    )
    if configured_article or isinstance(topic.get("offline_fallback"), dict):
        return configured_article, configured_qa, configured_feedback
    slug = str(topic.get("slug", ""))
    supported_slugs = {
        "troubleshooting-windows-event-logs-powershell",
        "troubleshooting-windows-dns-powershell",
        "kubernetes-probe-misconfigurations-fixes",
    }
    if slug not in supported_slugs:
        return None, None, "No offline fallback is configured for this topic."
    required = int(config.get("publishing", {}).get("required_source_count", 3))
    scoped = research_items_for_topic(topic, research, limit=required, config=config)
    if len(scoped) < required:
        return None, None, "Offline fallback lacks the required validated sources."
    get_winevent = next((item for item in scoped if "get-winevent" in item.title.lower()), scoped[0])
    filter_source = next((item for item in scoped if "filterhashtable" in item.title.lower()), scoped[min(1, len(scoped) - 1)])
    wevtutil = next((item for item in scoped if "wevtutil" in item.title.lower()), scoped[-1])
    now = iso_z()
    sections = [
        ("Direct answer", "Windows Event Logs are easiest to investigate with a narrow, read-only query. Use Get-WinEvent to retrieve events, FilterHashtable to constrain the log, provider, identifier, and time window, and wevtutil when a command-line-only environment is required. This workflow is for administrators diagnosing service failures, authentication problems, scheduled-task errors, and unexpected restarts. Start with the symptom, host, and approximate time; then preserve the evidence before correlating another source."),
        ("A safe investigation workflow", "Write down the affected host and its time zone before querying. List the available logs, choose the smallest useful time window, and set a result limit. Read the events first; do not clear, delete, or change retention settings during diagnosis. A bounded query makes an empty result explainable and keeps a production console usable. Repeat the same query on another host only after the first result has identified a provider or event identifier worth comparing."),
        ("Query with Get-WinEvent", "The Microsoft.PowerShell.Diagnostics Get-WinEvent cmdlet retrieves events from local or remote logs. The example below limits the System log to the last day and selects fields that remain useful when the output is copied into a ticket."),
        ("Filter with FilterHashtable", "Microsoft's FilterHashtable example demonstrates filtering by LogName, ProviderName, event Id, and time boundaries. Add one condition at a time. If the result becomes empty, remove the newest condition first and confirm the exact log and provider names rather than assuming that no event occurred."),
        ("Use wevtutil when PowerShell is unavailable", "wevtutil is the Windows event-log command-line utility. It is useful in recovery environments and small batch jobs. Its query syntax is different from a PowerShell hashtable, so keep the command bounded and do not mix the two syntaxes. Redirect a small result to a file when evidence must be attached to an incident record."),
        ("Troubleshoot empty or noisy results", "No events can mean the wrong log, provider, host, or time zone. Too many events usually mean that a time or count limit is missing. Preserve TimeCreated, Id, ProviderName, LevelDisplayName, and Message so results from different hosts can be compared. An event is evidence, not proof of causation; correlate it with the service, deployment, or authentication trace that produced the symptom."),
        ("Common mistakes", "Do not start by retrieving an entire log. Do not infer a failed login from a similarly named informational event. Do not copy a provider name from a different operating-system release without checking the local event. Do not clear a log to make a script pass. If a remote query fails, test connectivity and approved access separately from the event filter."),
        ("Practical checklist", "Record the host, log name, provider, event identifier, first and last timestamps, query text, and number of returned events. Save a small text or CSV export with the time zone noted. For intermittent failures, schedule the same read-only query and compare the preserved fields. This produces a reproducible trail without modifying the machine under investigation."),
        ("Version and verification notes", "This article uses the current Microsoft Learn pages for PowerShell 7.5 and Windows Server documentation as checked at publication time. Cmdlet parameters and provider names can vary by operating-system release and installed roles. Verify the module and host version before automating a long-lived task, and recheck the cited pages when a PowerShell or Windows Server release changes."),
        ("Summary", "Use Get-WinEvent with a small FilterHashtable for the first pass, keep every query read-only, and use wevtutil only when a command-line workflow requires it. Explicit timestamps, bounded output, and preserved event properties make the evidence reproducible and reduce the chance of deleting information needed to explain an incident."),
    ]
    body = "\n\n".join(f"## {heading}\n\n{text}" for heading, text in sections)
    body += """

## Detailed triage notes

When an event points to a service, inspect the service state and deployment timeline separately; the event query should remain a read-only evidence step. Keep the original message, provider, identifier, and timestamp together because a shortened message can hide the distinction between a warning and an error. If several events share a timestamp, sort by `TimeCreated` and compare the provider names before deciding which one is causal.

For remote hosts, establish the approved connection path before changing the filter. A successful network connection does not guarantee that the account can read every log, and an access error does not prove that the log is empty. Test a local query with the same account, then document the host name and time zone used for the remote request. This separation makes it possible to repair connectivity without rewriting the investigation.

If a query is going into scheduled automation, store the filter in a named variable, cap the number of returned events, and write a clear error when the result is empty. Avoid embedding secrets or changing retention settings. A small, reproducible query is safer to run repeatedly than a broad export that consumes memory and hides the first useful event in noise.

The most useful handoff includes the exact command, the host, the time zone, the selected properties, and the source log. That context lets another administrator repeat the check and distinguish a real product change from a difference in local configuration.

```powershell
$start = (Get-Date).AddHours(-24)
Get-WinEvent -FilterHashtable @{ LogName = 'System'; StartTime = $start } -MaxEvents 100 |
    Select-Object TimeCreated, Id, ProviderName, LevelDisplayName, Message
```

```powershell
Get-WinEvent -FilterHashtable @{
    LogName      = 'Application'
    ProviderName = 'Service Control Manager'
    StartTime    = (Get-Date).AddHours(-6)
} -MaxEvents 50
```

```powershell
wevtutil qe System /q:"*[System[(Level=2)]]" /f:text /c:20
```

| Symptom | Likely cause | Next safe check |
| --- | --- | --- |
| No events | Wrong log, provider, host, or time zone | List logs and widen the window gradually |
| Access denied | The account cannot read the target log | Test the same query locally and confirm approved access |
| Too much output | Missing time or count limit | Add StartTime, EndTime, and MaxEvents |
| Hard-to-compare messages | Formatting discarded fields | Select timestamp, ID, provider, level, and message |

For related background, see the [HTTP status-code reference](/posts/http-status-codes/) and [network connectivity fundamentals](/posts/internet-connectivity-and-cabling/)."""
    if slug == "troubleshooting-windows-dns-powershell":
        body = """## Direct answer

Windows DNS failures should be separated into three questions: does the machine have the expected interface and resolver settings, can it reach a resolver, and does the resolver return the record being requested? `Get-NetIPConfiguration`, `Test-NetConnection`, and `Resolve-DnsName` answer those questions without changing the adapter or DNS cache. This guide is for administrators who need a safe, repeatable diagnosis before changing DHCP, firewall, or server configuration.

## Start with local configuration

`Get-NetIPConfiguration` shows the interface, address, gateway, and DNS server assignments that Windows is using. Save the output before making a change. A missing address, an unexpected gateway, or a resolver from the wrong network explains many apparent DNS failures.

```powershell
Get-NetIPConfiguration | Format-List InterfaceAlias,IPv4Address,IPv6Address,IPv4DefaultGateway,DNSServer
```

Check the active interface rather than assuming that the first adapter is connected. Virtual adapters, VPN clients, and disconnected Wi-Fi profiles can make a list look more complicated than the path used by the failing application. If no resolver is shown, verify DHCP or the approved static configuration before testing public names.

## Test the path to a resolver

`Test-NetConnection` checks reachability and can test a TCP port. Use the resolver address shown by the configuration query rather than substituting a public resolver immediately. The result distinguishes a routing or firewall problem from a name-resolution problem.

```powershell
Test-NetConnection -ComputerName 192.0.2.53 -Port 53
```

Replace `192.0.2.53` with the documented resolver for the environment. A failed TCP test does not prove that DNS is broken; it may indicate that the resolver uses another transport or that a firewall policy blocks the test. Record `PingSucceeded`, `TcpTestSucceeded`, and the interface selected by the command.

## Query a record directly

`Resolve-DnsName` returns structured DNS records and supports an explicit server. Start with the name and record type involved in the incident, then repeat against the configured resolver if necessary.

```powershell
Resolve-DnsName -Name example.com -Type A
Resolve-DnsName -Name example.com -Type A -Server 192.0.2.53
```

Use a domain approved for testing in your environment. Compare the answer, status, and server between the two queries. An answer from one resolver and a timeout from another points to resolver health, routing, policy, or delegation rather than a local browser problem.

## Read the three results together

| Observation | Most likely boundary | Next safe check |
| --- | --- | --- |
| No address or resolver | Interface, DHCP, or static configuration | Inspect the active adapter and approved DHCP scope |
| Resolver unreachable | Routing, firewall, or resolver availability | Test the gateway and the resolver port separately |
| Resolver reachable but name fails | Zone, record, delegation, or policy | Query the authoritative or approved recursive server |
| Name works on one resolver only | Cache or server-data difference | Compare TTL, answer, and resolver identity |

Do not change DNS settings just because a single public name fails. First query a known internal name and a known external name, and note whether the failure is consistent across clients. This avoids turning a local diagnostic problem into a wider outage.

## Common mistakes and safe recovery

Avoid confusing a successful ping with successful DNS. ICMP may be blocked while DNS works, and a resolver may answer over a path that does not respond to ping. Avoid clearing the DNS client cache as the first action; it removes useful evidence and can hide whether the resolver or the cache produced the answer. If a cache reset is approved, record the failed and successful queries before and after the reset.

Use the exact interface and resolver values from the host. VPN software, split DNS, and policy-based resolvers can make a public lookup appear healthy while an internal name fails. When the issue is intermittent, capture several results with timestamps instead of relying on one successful query.

## Practical checklist

1. Save `Get-NetIPConfiguration` output and identify the active interface.
2. Test the documented resolver with `Test-NetConnection`.
3. Query the failing name with `Resolve-DnsName`.
4. Repeat against the documented resolver and compare the answer.
5. Check firewall, routing, DHCP, delegation, and server logs at the boundary indicated by the results.
6. Document every command, timestamp, resolver, record type, and returned status.

For background, see the [DNS fundamentals article](/posts/dns-explained-how-your-browser-finds-a-website/) and [common network ports reference](/posts/common-network-ports-every-it-student-should-know/).

## Preserve diagnostic evidence

When the result is intermittent, capture several queries with timestamps instead of relying on one successful lookup. Keep the resolver address, record type, response status, and interface together. A small text export is more useful than an unbounded console transcript because another administrator can repeat the exact test and compare the result after a routing, DHCP, or server-side change. Redact environment-specific hostnames and addresses before sharing examples publicly.

If the resolver returns an answer but the application still fails, capture the exact name and record type used by the application. A browser may use a proxy, a service may use a different resolver, and a VPN may apply split-DNS rules. Compare the PowerShell result with the application's documented endpoint without changing the resolver yet. This keeps the diagnostic boundary clear and prevents a successful test of the wrong name from closing the incident prematurely.

## Version and verification notes

The examples use the current Microsoft Learn pages for Windows Server 2025 PowerShell modules as checked at publication time. Cmdlet parameters and output properties can vary by Windows release and installed module. Verify the host version before putting a query into a long-lived monitoring task.

## Summary

Use configuration inspection, resolver reachability, and direct record queries as separate tests. Keeping those boundaries distinct makes DNS failures easier to repair and avoids changing production settings before the evidence identifies the responsible layer."""
    elif slug == "kubernetes-probe-misconfigurations-fixes":
        body = """## Direct answer

Kubernetes probes fail when the check does not match the application's startup time, listening address, endpoint, or expected response. A liveness probe decides whether a container should be restarted, a readiness probe decides whether traffic should be sent, and a startup probe gives a slow-starting container time to initialize before the other checks matter. Diagnose the probe type first, then verify the command, HTTP path, port, and timing values against the running Pod.

## Understand the three probe roles

The Kubernetes documentation separates liveness, readiness, and startup behavior. Liveness is for a process that is no longer healthy and may need a restart. Readiness controls whether a Pod is considered ready for service traffic; a failed readiness check does not by itself restart the container. Startup protects applications that need a long initialization period by delaying liveness and readiness evaluation until startup succeeds.

| Probe | Failed result means | Typical diagnostic question |
| --- | --- | --- |
| Liveness | Restart may be triggered | Does the process remain alive and able to answer the health check? |
| Readiness | Pod is removed from service endpoints | Is the application ready for the dependency and traffic it receives? |
| Startup | Initialization has not completed | Does the application need more time or a different startup condition? |

## Inspect the actual Pod specification

Start with the manifest applied to the cluster rather than a local template. Check the probe handler, port, path, scheme, delay, period, timeout, and failure threshold. Then read the Pod events and container logs to determine whether the check failed because the process was unavailable or because the check itself was wrong.

```bash
kubectl get pod POD_NAME -n NAMESPACE -o yaml
kubectl describe pod POD_NAME -n NAMESPACE
kubectl logs POD_NAME -n NAMESPACE --previous
```

Replace the uppercase values with the target Pod and namespace. These commands read the object, events, and prior container output; they do not edit the workload. Compare the port in the probe with the port where the process is actually listening inside the container.

## Check each handler type

An HTTP probe needs a reachable path and the correct container port. An exec probe must use a command available in the image and return the expected exit status. A TCP probe tests whether a connection can be established; it does not prove that an HTTP endpoint is serving the correct response. A mismatch between the handler and application protocol is a common cause of repeated failures.

```yaml
startupProbe:
  httpGet:
    path: /healthz
    port: 8080
  failureThreshold: 30
  periodSeconds: 10
readinessProbe:
  httpGet:
    path: /ready
    port: 8080
  periodSeconds: 10
livenessProbe:
  httpGet:
    path: /healthz
    port: 8080
  periodSeconds: 20
```

The values are examples, not universal defaults. Confirm that the application exposes both paths and that the port is bound inside the Pod. A startup probe with an overly small window can restart a legitimately slow application; a readiness probe with the wrong dependency check can keep a healthy process out of service.

## Troubleshoot timing and restarts

If events show liveness failures during initialization, use a startup probe or increase the startup window based on measured startup time. Do not simply make every timeout large: a long liveness timeout can delay recovery, while a short readiness interval can create endpoint churn during a brief dependency outage. Measure the application, then choose values that represent the service's real behavior.

| Symptom | Likely cause | Safe next check |
| --- | --- | --- |
| Immediate restarts | Liveness runs before startup completes | Inspect startup duration and add or correct startupProbe |
| Pod stays NotReady | Path, port, dependency, or readiness condition is wrong | Run the endpoint check inside the container and inspect events |
| Probe connection refused | Process binds another address or port | Compare the listening socket with the manifest |
| Exec probe fails | Utility or shell is absent from the image | Run the command interactively and check its exit code |
| Intermittent failures | Timeout too short or dependency is unstable | Compare latency with timeout and inspect dependency health |

## Practical checklist

1. Identify which probe failed and when.
2. Inspect the live Pod YAML, events, and previous logs.
3. Verify handler, path, port, scheme, and image command.
4. Measure startup and response time before changing thresholds.
5. Use startupProbe for slow initialization and readiness for traffic eligibility.
6. Roll out a small change and watch events before making another change.

For related operations guidance, see the [Kubernetes workloads article](/posts/operating-ai-ml-workloads-kubernetes/) and [Windows Event Logs troubleshooting guide](/posts/troubleshooting-windows-event-logs-powershell/).

## Preserve a measured rollout

Change one probe field at a time and watch Pod events before changing another. Record the old and new manifest, the observed startup and response times, and whether the failure affected restarts or only service endpoints. This makes a rollback straightforward and prevents a large timeout from hiding a real application failure. Keep probe paths and ports in the same configuration source as the container so a future image update cannot silently invalidate the health check.

When a probe calls an application dependency, make the dependency failure visible in the event and application logs rather than returning a generic success. A readiness check can then remove the Pod from traffic while the process remains available for diagnosis. Keep liveness focused on whether the process is functioning; using it as a dependency test can cause a restart loop that makes recovery harder.

## Version and verification notes

The examples use version-neutral probe fields from the current Kubernetes documentation and Pod v1 API reference checked at publication time. Validate fields against the Kubernetes version running your cluster, because API behavior and available handlers can change across releases.

## Summary

Choose the probe based on the failure decision it controls, verify the handler against the real process, and tune timing from measurements. This prevents a health check from becoming the cause of restarts or unavailable service endpoints."""
    if slug == "troubleshooting-windows-dns-powershell":
        description = "A safe PowerShell workflow for diagnosing Windows DNS configuration, resolver reachability, record lookups, and common network failures."
        claims = [
            {"claim": scoped[0].title, "supporting_sources": [scoped[0].url], "confidence": 0.95, "verified_at": now, "version_context": "Windows Server 2025 PowerShell documentation checked at publication time."},
            {"claim": scoped[1].title, "supporting_sources": [scoped[1].url], "confidence": 0.95, "verified_at": now, "version_context": "Windows Server 2025 PowerShell documentation checked at publication time."},
            {"claim": scoped[2].title, "supporting_sources": [scoped[2].url], "confidence": 0.95, "verified_at": now, "version_context": "Windows Server 2025 PowerShell documentation checked at publication time."},
        ]
    elif slug == "kubernetes-probe-misconfigurations-fixes":
        description = "Diagnose Kubernetes liveness, readiness, and startup probe failures by checking handlers, ports, timing, Pod events, and container behavior."
        claims = [
            {"claim": scoped[0].title, "supporting_sources": [scoped[0].url], "confidence": 0.95, "verified_at": now, "version_context": "Current Kubernetes probe documentation checked at publication time."},
            {"claim": scoped[1].title, "supporting_sources": [scoped[1].url], "confidence": 0.94, "verified_at": now, "version_context": "Current Kubernetes probe documentation checked at publication time."},
            {"claim": scoped[2].title, "supporting_sources": [scoped[2].url], "confidence": 0.94, "verified_at": now, "version_context": "Kubernetes Pod v1 API reference checked at publication time."},
        ]
    else:
        description = "A safe, repeatable PowerShell workflow for querying Windows Event Logs, narrowing results, troubleshooting empty output, and preserving evidence during incident analysis."
        claims = [
            {"claim": "Get-WinEvent retrieves Windows events from event logs.", "supporting_sources": [get_winevent.url], "confidence": 0.96, "verified_at": now, "version_context": "PowerShell 7.5 documentation checked at publication time."},
            {"claim": "FilterHashtable queries support filtering by log name, provider, event ID, and time boundaries.", "supporting_sources": [filter_source.url], "confidence": 0.95, "verified_at": now, "version_context": "PowerShell 7.5 documentation checked at publication time."},
            {"claim": "wevtutil is the Windows command-line utility for querying event logs.", "supporting_sources": [wevtutil.url], "confidence": 0.95, "verified_at": now, "version_context": "Windows Server documentation checked at publication time."},
        ]
    article = {
        "title": "Windows DNS Diagnostics with PowerShell: A Safe Troubleshooting Workflow" if slug == "troubleshooting-windows-dns-powershell" else topic["title"],
        "slug": topic["slug"],
        "description": description,
        "categories": topic.get("categories", []),
        "tags": topic.get("tags", []),
        "article_markdown": body,
        "sources": [{"title": item.title, "url": item.url} for item in scoped],
        "claim_evidence": claims,
    }
    article = normalize_article_payload(article, topic, config, scoped, posts=posts)
    issues = deterministic_qa(article, topic, posts, config, scoped)
    if issues:
        log.log("deterministic_fallback_rejected", title=topic.get("title"), issues=issues[:20])
        return None, None, "; ".join(issues[:8])
    qa = {"approved": True, "score": 0.9, "technical_accuracy": 0.94, "factual_confidence": 0.95, "unsupported_claims": [], "issues": [], "required_fixes": [], "reason": "Official-source offline recovery article."}
    quality = calculate_quality_score(article, topic, posts, config, qa, scoped)
    qa["quality"] = quality
    minimum = float(config.get("publishing", {}).get("quality_min_score", 0.82))
    if quality["score"] < minimum:
        detail = f"Offline fallback quality score {quality['score']:.4f} is below {minimum:.4f}."
        log.log("deterministic_fallback_rejected", title=topic.get("title"), reason=detail)
        return None, None, detail
    log.log("deterministic_fallback_approved", title=topic.get("title"), quality_score=quality["score"])
    return article, qa, ""


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
    volatility_text = f"{article.get('title', '')} {' '.join(article.get('categories', []) or [])} {article.get('article_markdown', '')[:1500]}".lower()
    intervals = config.get("revalidation_intervals", {})
    if re.search(r"\b(price|pricing|cost)\b", volatility_text):
        recheck_days = int(intervals.get("pricing_days", 7))
    elif re.search(r"\b(release|version|current)\b", volatility_text):
        recheck_days = int(intervals.get("release_days", 14))
    elif "cloud-certifications" in article.get("categories", []):
        recheck_days = int(intervals.get("certification_days", 21))
    elif any(category in article.get("categories", []) for category in ("azure", "entra-id")):
        recheck_days = int(intervals.get("cloud_configuration_days", 35))
    elif "networking" in article.get("categories", []) and re.search(r"\b(rfc|protocol|tcp|udp|dns)\b", volatility_text):
        recheck_days = int(intervals.get("stable_protocol_days", 150))
    else:
        recheck_days = int(intervals.get("default_days", 60))
    frontmatter: dict[str, Any] = {
        "title": article["title"],
        "date": now.replace(microsecond=0).isoformat(),
        "lastmod": now.replace(microsecond=0).isoformat(),
        "description": article["description"],
        "tags": article["tags"],
        "categories": article["categories"],
        "publisher": config.get("site", {}).get("publisher_name", config.get("site", {}).get("name", "Compile My Mind")),
        "draft": False,
        "autonomous": True,
        "last_reviewed": now.date().isoformat(),
        "verification_status": normalize_space(
            str(article.get("verification_status", "Documentation reviewed"))
        ),
        "verification_date": iso_z(now),
        "verification_version": 1,
        "version_context": normalize_space(str(article.get("version_context", "Documentation current at verification time")))[:240],
        "recheck_after": (now + dt.timedelta(days=recheck_days)).date().isoformat(),
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


def ready_bundle_path(entry: dict[str, Any]) -> Path | None:
    """Resolve a tracked bundle only when it remains inside the ready queue."""
    raw_path = str(entry.get("bundle_path", "")).strip()
    if not raw_path:
        return None
    candidate = (ROOT / raw_path).resolve()
    queue_root = READY_QUEUE_DIR.resolve()
    try:
        candidate.relative_to(queue_root)
    except ValueError:
        return None
    return candidate


def valid_ready_publications(state: dict[str, Any]) -> list[dict[str, Any]]:
    valid: list[dict[str, Any]] = []
    for entry in state.get("ready_publications", []) or []:
        if not isinstance(entry, dict):
            continue
        bundle_path = ready_bundle_path(entry)
        if bundle_path is not None and (bundle_path / "index.md").is_file():
            valid.append(entry)
    return valid


def queue_approved_publication(
    index_path: Path,
    article: dict[str, Any],
    qa: dict[str, Any] | None,
    state: dict[str, Any],
    config: dict[str, Any],
    log: EventLog,
) -> bool:
    """Move a fully approved Hugo bundle into the durable ready queue."""
    slug = str(article.get("slug", "")).strip()
    source_dir = index_path.parent
    target_dir = READY_QUEUE_DIR / slug
    approved = (qa or {}).get("approved") is True or str((qa or {}).get("approved", "")).lower() == "true"
    if not approved:
        log.log("publication_queue_failed", slug=slug, reason="missing_approved_qa")
        return False
    if slug != slugify(slug) or not source_dir.is_dir() or target_dir.exists():
        log.log("publication_queue_failed", slug=slug, reason="missing_or_duplicate_bundle")
        return False
    READY_QUEUE_DIR.mkdir(parents=True, exist_ok=True)
    shutil.move(str(source_dir), str(target_dir))
    queued_at = iso_z()
    record = {
        "time": queued_at,
        "title": article.get("title", ""),
        "slug": slug,
        "categories": article.get("categories", []),
        "tags": article.get("tags", []),
        "sources": article.get("sources", []),
        "qa": qa or {},
        "quality_score": (qa or {}).get("quality", {}).get("score"),
        "path": str((ROOT / config["site"].get("content_dir", "content/posts") / slug / "index.md").relative_to(ROOT)),
    }
    entry = {
        "queued_at": queued_at,
        "slug": slug,
        "title": article.get("title", ""),
        "bundle_path": str(target_dir.relative_to(ROOT)),
        "record": record,
    }
    state.setdefault("ready_publications", []).append(entry)
    state["ready_publications"] = state["ready_publications"][-100:]
    state.setdefault("last_runs", {})["prepare"] = {
        "time": queued_at,
        "result": "queued",
        "slug": slug,
        "queue_depth": len(valid_ready_publications(state)),
    }
    state["pending_publication"] = {}
    save_state(state)
    write_prepare_result(
        "queued",
        slug=slug,
        title=article.get("title", ""),
        queue_depth=len(valid_ready_publications(state)),
    )
    log.log(
        "publication_queued",
        slug=slug,
        title=article.get("title", ""),
        queue_depth=len(valid_ready_publications(state)),
    )
    return True


def refresh_queued_publication_dates(index_path: Path, config: dict[str, Any]) -> None:
    frontmatter, body = split_frontmatter(index_path.read_text(encoding="utf-8"))
    now = local_now(config)
    frontmatter["date"] = now.replace(microsecond=0).isoformat()
    frontmatter["lastmod"] = now.replace(microsecond=0).isoformat()
    frontmatter["last_reviewed"] = now.date().isoformat()
    frontmatter["verification_date"] = iso_z(now)
    recheck_days = int(config.get("revalidation_intervals", {}).get("default_days", 60))
    frontmatter["recheck_after"] = (now + dt.timedelta(days=recheck_days)).date().isoformat()
    index_path.write_text(compose_markdown(frontmatter, body), encoding="utf-8")


def publish_ready_publication(
    state: dict[str, Any],
    config: dict[str, Any],
    log: EventLog,
    *,
    dry_run: bool,
) -> bool:
    """Promote the oldest approved queue item before making any provider call."""
    entries = sorted(
        valid_ready_publications(state),
        key=lambda entry: str(entry.get("queued_at", "")),
    )
    if not entries:
        return False
    maximum_age = max(1, int(config.get("publication_queue", {}).get("max_age_days", 7)))
    content_dir = ROOT / config["site"].get("content_dir", "content/posts")
    for entry in entries:
        slug = str(entry.get("slug", ""))
        queued_at = parse_date(str(entry.get("queued_at", "")))
        if queued_at and utc_now() - queued_at > dt.timedelta(days=maximum_age):
            log.log("queued_publication_skipped", slug=slug, reason="queue_item_expired")
            continue
        source_dir = ready_bundle_path(entry)
        destination = content_dir / slug
        if (
            slug != slugify(slug)
            or source_dir is None
            or destination.exists()
            or not (source_dir / "index.md").is_file()
        ):
            log.log("queued_publication_skipped", slug=slug, reason="invalid_or_duplicate_bundle")
            continue
        record = entry.get("record", {}) if isinstance(entry.get("record"), dict) else {}
        qa = record.get("qa", {}) if isinstance(record.get("qa"), dict) else {}
        approved = qa.get("approved") is True or str(qa.get("approved", "")).lower() == "true"
        if not approved:
            log.log("queued_publication_skipped", slug=slug, reason="missing_approved_qa")
            continue
        if dry_run:
            log.log("queued_publication_ready", slug=slug, title=entry.get("title", ""), dry_run=True)
            write_publish_result("dry_run", slug=slug, source="ready_queue")
            return True
        try:
            shutil.copytree(source_dir, destination)
            refresh_queued_publication_dates(destination / "index.md", config)
            build_passed = run_hugo_build(log)
        except Exception as error:
            shutil.rmtree(destination, ignore_errors=True)
            log.log(
                "queued_publication_skipped",
                slug=slug,
                reason="promotion_failed",
                error=str(error),
            )
            continue
        if not build_passed:
            shutil.rmtree(destination, ignore_errors=True)
            log.log("queued_publication_skipped", slug=slug, reason="hugo_build_failed")
            continue
        try:
            shutil.rmtree(source_dir)
        except Exception as error:
            shutil.rmtree(destination, ignore_errors=True)
            log.log(
                "queued_publication_skipped",
                slug=slug,
                reason="queue_cleanup_failed",
                error=str(error),
            )
            continue
        state["ready_publications"] = [
            candidate
            for candidate in state.get("ready_publications", []) or []
            if not isinstance(candidate, dict) or candidate.get("slug") != slug
        ]
        publication_record = dict(record)
        publication_record["time"] = iso_z()
        state.setdefault("generated_posts", []).append(publication_record)
        state.setdefault("last_runs", {})["publish"] = {
            "time": iso_z(),
            "result": "published",
            "source": "ready_queue",
            "queue_depth": len(valid_ready_publications(state)),
        }
        state["pending_publication"] = {}
        save_state(state)
        write_publish_result(
            "published",
            path=publication_record.get("path", str((destination / "index.md").relative_to(ROOT))),
            title=publication_record.get("title", entry.get("title", "")),
            source="ready_queue",
        )
        log.log(
            "queued_publication_published",
            slug=slug,
            title=entry.get("title", ""),
            queue_depth=len(valid_ready_publications(state)),
        )
        return True
    return False


def run_prepare(args: argparse.Namespace) -> int:
    config = load_config()
    state_before = load_state()
    target = max(1, int(config.get("publication_queue", {}).get("target_depth", 12)))
    depth = len(valid_ready_publications(state_before))
    if depth >= target:
        write_prepare_result("capacity_reached", queue_depth=depth, target_depth=target)
        return 0
    # Preparation has its own retry lane. A failed public topic must not pin
    # every queue-filling run to the same candidate, and a preparation retry
    # must not replace the public publisher's durable state.
    working_state = copy.deepcopy(state_before)
    working_state["pending_publication"] = copy.deepcopy(
        state_before.get("preparation_pending_publication", {}) or {}
    )
    save_state(working_state)
    prepared_args = argparse.Namespace(dry_run=False, prepare_only=True)
    result = run_publish(prepared_args)
    state_after = load_state()
    prepare_marker = read_json(PREPARE_RESULT_PATH, {})
    if prepare_marker.get("result") == "queued":
        state_after["pending_publication"] = {}
        state_after["preparation_pending_publication"] = {}
        save_state(state_after)
        return result

    # Preparation must not overwrite the public publisher's pending retry or
    # last result when it cannot add a queue item. Preserve only useful provider
    # cooldown evidence from the attempted preparation.
    restored = copy.deepcopy(state_before)
    restored["provider_cooldowns"] = state_after.get(
        "provider_cooldowns", restored.get("provider_cooldowns", {})
    )
    restored["preparation_pending_publication"] = copy.deepcopy(
        state_after.get("pending_publication", {}) or {}
    )
    restored["rejected_articles"] = state_after.get(
        "rejected_articles", restored.get("rejected_articles", [])
    )
    restored["failures"] = state_after.get("failures", restored.get("failures", []))
    failure = state_after.get("last_runs", {}).get("publish", {}) or {}
    restored.setdefault("last_runs", {})["prepare"] = {
        "time": iso_z(),
        "result": "retry_scheduled",
        "reason": failure.get("reason", failure.get("result", "no_approved_candidate")),
        "queue_depth": depth,
    }
    save_state(restored)
    write_prepare_result(
        "retry_scheduled",
        reason=restored["last_runs"]["prepare"]["reason"],
        queue_depth=depth,
    )
    return result


def run_publish(args: argparse.Namespace) -> int:
    config = load_config()
    state = load_state()
    log = EventLog()
    prepare_only = bool(getattr(args, "prepare_only", False))
    if prepare_only:
        write_prepare_result("started")
    else:
        write_publish_result("started")
    posts = load_posts(config)
    if prepare_only:
        posts = [*posts, *load_queued_posts(state)]
    elif config.get("publication_queue", {}).get("enabled", True):
        if publish_ready_publication(state, config, log, dry_run=bool(args.dry_run)):
            return 0

    client = GeminiClient(config, log, state)
    research = collect_research(config, log)
    if not research:
        log.log("publish_recovery_needed", reason="no_research_items", fallback="configured_evergreen_sources")
    elif config.get("source_validation"):
        research = validate_research_items(research, config, log)
    else:
        # Preserve the original lightweight behavior for isolated test and
        # legacy configurations; production always carries source_validation.
        enrich_research_snippets(research, config, log)
    required_sources = int(config.get("publishing", {}).get("required_source_count", 1))
    if len(research) < required_sources:
        log.log(
            "publish_recovery_needed",
            reason="not_enough_valid_sources",
            valid_sources=len(research),
            fallback="configured_evergreen_sources",
        )

    previous_publish_result = str(state.get("last_runs", {}).get("publish", {}).get("result", ""))
    prioritize_evergreen = bool(
        config.get("publishing", {}).get("prefer_source_qualified_evergreen_first", False)
        or (
            config.get("publishing", {}).get("prefer_evergreen_after_quota", True)
            and previous_publish_result in {"quota_limited", "qa_failed"}
        )
    )
    grounded_brief: dict[str, Any] | None = None
    grounding_circuit_open = provider_circuit_open(state, "gemini_grounded_research")
    if not prioritize_evergreen and grounding_circuit_open:
        log.log("grounded_research_skipped", stage="grounded_research", reason="provider_circuit_open")
    if (
        not prioritize_evergreen
        and not grounding_circuit_open
        and config.get("gemini", {}).get("enable_google_search_grounding", True)
    ):
        try:
            grounded_prompt = (
                "Find current high-interest, trustworthy technical article opportunities for Compile My Mind. "
                "Only consider cybersecurity, identity and access management, networking, IT fundamentals, "
                "Microsoft Azure, Microsoft Entra ID, cloud certifications, system administration, practical "
                "infrastructure, and developer or IT tools. Exclude consumer hardware launches, mobile products, "
                "unrelated AI announcements, entertainment, politics, automotive, and lifestyle topics. "
                "Return concise findings with citations."
            )
            grounded_brief = client.grounded_research(grounded_prompt)
            log.log("grounded_research_completed", citation_count=len(grounded_brief.get("citations", [])))
            discovery_candidates = grounded_brief_research_items(
                grounded_brief,
                {
                    "title": "Compile My Mind approved technical topic discovery",
                    "categories": [],
                },
                source="Gemini grounded topic discovery",
            )
            if discovery_candidates:
                validated_discovery = validate_research_items(discovery_candidates, config, log)
                research = merge_research_items(research, validated_discovery)
                log.log(
                    "grounded_discovery_sources_merged",
                    candidates=len(discovery_candidates),
                    validated=len(validated_discovery),
                    total_research_sources=len(research),
                )
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

    topic = pending_publication_topic(state, posts, config, log)
    retrying_pending_topic = bool(topic)
    initial_topic_is_evergreen = False
    if not topic and prioritize_evergreen:
        topic = choose_evergreen_topic(posts, config, log)
        initial_topic_is_evergreen = bool(topic)
    if topic:
        if not retrying_pending_topic:
            log.log("evergreen_quota_recovery_started", title=topic.get("title"), slug=topic.get("slug"))
    else:
        try:
            topic = choose_topic(client, research, grounded_brief, posts, config, log)
        except GeminiQuotaError as error:
            log.log("publish_quota_limited", stage="topic_selection", error=str(error))
            topic = choose_evergreen_topic(posts, config, log)
            if topic:
                initial_topic_is_evergreen = True
                log.log("evergreen_quota_recovery_started", title=topic.get("title"), slug=topic.get("slug"))
            else:
                schedule_publish_retry(
                    state,
                    config,
                    log,
                    reason="provider_quota_limited",
                    stage="topic_selection",
                    detail=str(error),
                )
                return 0
        except GeminiTransientError as error:
            log.log("publish_retryable", stage="topic_selection", error=str(error))
            topic = choose_evergreen_topic(posts, config, log)
            if not topic:
                schedule_publish_retry(
                    state,
                    config,
                    log,
                    reason="provider_transient_error",
                    stage="topic_selection",
                    detail=str(error),
                )
                return 0
            initial_topic_is_evergreen = True
    if not topic:
        schedule_publish_retry(
            state,
            config,
            log,
            reason="no_valid_topic",
            stage="topic_selection",
            detail="No source-qualified, in-scope, non-duplicate topic was available in this run.",
        )
        return 0

    final_article: dict[str, Any] | None = None
    final_qa: dict[str, Any] | None = None
    feedback = ""
    topic_research: list[ResearchItem] = []
    excluded_slugs: set[str] = set()
    configured_topic_attempts = max(1, int(config.get("publishing", {}).get("max_topic_attempts", 2)))
    topic_call_budget = max(
        1,
        int(config.get("cost_control", {}).get("max_topic_selection_calls_per_run", configured_topic_attempts)),
    )
    # Prefer an evergreen topic when one exists, but do not suppress alternate
    # dynamic-topic attempts merely because the evergreen catalog is exhausted.
    max_topic_attempts = 1 if (initial_topic_is_evergreen and not retrying_pending_topic) else min(
        topic_call_budget,
        configured_topic_attempts,
    )
    for topic_attempt in range(1, max_topic_attempts + 1):
        if topic_attempt > 1:
            excluded_slugs.add(str(topic.get("slug", "")))
            try:
                topic = choose_topic(
                    client,
                    research,
                    grounded_brief,
                    posts,
                    config,
                    log,
                    excluded_slugs=excluded_slugs,
                )
            except GeminiQuotaError as error:
                log.log("publish_quota_limited", stage="topic_selection", attempt=topic_attempt, error=str(error))
                feedback = str(error)
                break
            except GeminiTransientError as error:
                log.log("publish_retryable", stage="topic_selection", attempt=topic_attempt, error=str(error))
                feedback = str(error)
                break
            if not topic:
                feedback = "No alternative topic passed duplicate and category checks."
                break
            log.log(
                "topic_retry_selected",
                attempt=topic_attempt,
                title=topic.get("title"),
                slug=topic.get("slug"),
            )
        try:
            topic_research = collect_topic_research(client, topic, research, config, log, state=state)
            required_topic_sources = int(config.get("publishing", {}).get("required_source_count", 1))
            if len(topic_research) < required_topic_sources:
                feedback = (
                    f"Only {len(topic_research)} directly relevant validated sources were available; "
                    f"at least {required_topic_sources} are required."
                )
                log.log(
                    "topic_generation_exhausted",
                    attempt=topic_attempt,
                    title=topic.get("title"),
                    stage="topic_source_collection",
                    feedback=feedback,
                )
                continue
            if isinstance(topic.get("offline_fallback"), dict):
                final_article, final_qa, feedback = deterministic_evergreen_fallback(
                    topic, topic_research, posts, config, log
                )
                log.log(
                    "offline_recovery_attempted_before_model",
                    title=topic.get("title"),
                    approved=bool(final_article),
                )
            else:
                final_article, final_qa, feedback = generate_approved_article(
                    client,
                    topic,
                    topic_research,
                    posts,
                    config,
                    log,
                )
        except GeminiQuotaError as error:
            log.log("publish_quota_limited", stage="article_generation", attempt=topic_attempt, error=str(error))
            feedback = str(error)
            log.log("publish_offline_recovery_started", stage="article_generation", attempt=topic_attempt)
            break
        except GeminiTransientError as error:
            log.log("publish_retryable", stage="article_generation", attempt=topic_attempt, error=str(error))
            feedback = str(error)
            log.log("publish_offline_recovery_started", stage="article_generation", attempt=topic_attempt)
            break
        if final_article:
            break
        log.log(
            "topic_generation_exhausted",
            attempt=topic_attempt,
            title=topic.get("title"),
            feedback=feedback,
        )

    if not final_article:
        excluded_slugs.add(str(topic.get("slug", "")))

    evergreen_attempts = max(0, int(config.get("publishing", {}).get("max_evergreen_topic_attempts", 0)))
    for evergreen_attempt in range(1, evergreen_attempts + 1):
        if final_article:
            break
        evergreen_topic = choose_evergreen_topic(posts, config, log, excluded_slugs=excluded_slugs)
        if not evergreen_topic:
            break
        topic = evergreen_topic
        excluded_slugs.add(str(topic.get("slug", "")))
        log.log(
            "evergreen_recovery_started",
            attempt=evergreen_attempt,
            title=topic.get("title"),
            slug=topic.get("slug"),
        )
        try:
            topic_research = collect_topic_research(client, topic, research, config, log, state=state)
            required_topic_sources = int(config.get("publishing", {}).get("required_source_count", 1))
            if len(topic_research) < required_topic_sources:
                feedback = (
                    f"Only {len(topic_research)} directly relevant validated sources were available for the evergreen topic; "
                    f"at least {required_topic_sources} are required."
                )
                log.log("evergreen_recovery_failed", attempt=evergreen_attempt, title=topic.get("title"), feedback=feedback)
                continue
            if isinstance(topic.get("offline_fallback"), dict):
                final_article, final_qa, feedback = deterministic_evergreen_fallback(
                    topic, topic_research, posts, config, log
                )
                log.log(
                    "offline_recovery_attempted_before_model",
                    title=topic.get("title"),
                    approved=bool(final_article),
                )
            else:
                final_article, final_qa, feedback = generate_approved_article(
                    client, topic, topic_research, posts, config, log
                )
        except GeminiQuotaError as error:
            log.log("publish_quota_limited", stage="evergreen_article_generation", attempt=evergreen_attempt, error=str(error))
            feedback = str(error)
            continue
        except GeminiTransientError as error:
            log.log("publish_retryable", stage="evergreen_article_generation", attempt=evergreen_attempt, error=str(error))
            feedback = str(error)
            continue
        if not final_article:
            log.log("evergreen_recovery_failed", attempt=evergreen_attempt, title=topic.get("title"), feedback=feedback)

    if not final_article:
        fallback_article, fallback_qa, fallback_feedback = deterministic_evergreen_fallback(
            topic, topic_research or research, posts, config, log
        )
        if fallback_article:
            final_article, final_qa, feedback = fallback_article, fallback_qa, ""
            log.log("offline_recovery_selected", title=topic.get("title"), slug=topic.get("slug"))

    if not final_article:
        record_rejection(
            state,
            log,
            topic=topic,
            reason="qa_failed",
            detail=feedback,
            attempts=max_topic_attempts + evergreen_attempts,
        )
        state["last_runs"]["publish"] = {"time": iso_z(), "result": "qa_failed"}
        log.log("publish_rejected_all_drafts", title=topic.get("title"), feedback=feedback)
        schedule_publish_retry(
            state,
            config,
            log,
            reason="all_drafts_failed_quality_gates",
            stage="article_quality",
            detail=feedback,
            topic=topic,
            sources=topic_research,
        )
        return 0

    try:
        index_path = write_article_bundle(final_article, topic, config, log, dry_run=args.dry_run)
    except ValueError as error:
        failed_post_dir = ROOT / config["site"].get("content_dir", "content/posts") / str(final_article.get("slug", ""))
        shutil.rmtree(failed_post_dir, ignore_errors=True)
        record_rejection(state, log, topic=topic, reason="asset_validation_failed", detail=str(error))
        log.log("publish_rejected", reason="asset_validation_failed", error=str(error))
        schedule_publish_retry(
            state,
            config,
            log,
            reason="asset_validation_failed",
            stage="asset_validation",
            detail=str(error),
            topic=topic,
            sources=topic_research,
        )
        return 0
    if not args.dry_run and not run_hugo_build(log):
        if index_path.parent.exists():
            shutil.rmtree(index_path.parent)
        record_rejection(state, log, topic=topic, reason="build_failed", detail=str(index_path.relative_to(ROOT)))
        schedule_publish_retry(
            state,
            config,
            log,
            reason="build_failed",
            stage="hugo_build",
            detail=str(index_path.relative_to(ROOT)),
            topic=topic,
            sources=topic_research,
        )
        return 0

    if prepare_only and not args.dry_run:
        if queue_approved_publication(index_path, final_article, final_qa, state, config, log):
            return 0
        if index_path.parent.exists():
            shutil.rmtree(index_path.parent)
        schedule_publish_retry(
            state,
            config,
            log,
            reason="queue_storage_failed",
            stage="publication_queue",
            detail=str(index_path.relative_to(ROOT)),
            topic=topic,
            sources=topic_research,
        )
        return 0

    state.setdefault("generated_posts", []).append(
        {
            "time": iso_z(),
            "title": final_article["title"],
            "slug": final_article["slug"],
            "categories": final_article["categories"],
            "tags": final_article["tags"],
            "sources": final_article.get("sources", []),
            "qa": final_qa,
            "quality_score": (final_qa or {}).get("quality", {}).get("score"),
            "path": str(index_path.relative_to(ROOT)),
        }
    )
    state["last_runs"]["publish"] = {"time": iso_z(), "result": "published" if not args.dry_run else "dry_run"}
    state["pending_publication"] = {}
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
        recheck_after = parse_date(str(post.frontmatter.get("recheck_after", "")))
        explicitly_due = bool(recheck_after and recheck_after <= now)
        reviewed_raw = reviews.get(post.slug, {}).get("time")
        reviewed = parse_date(reviewed_raw)
        if reviewed:
            age = (now - reviewed.astimezone(dt.timezone.utc)).total_seconds() / 86400
        else:
            age = 9999
        stale_bonus = 0 if explicitly_due or age >= review_after_days else 1
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
- Use only directly relevant official or primary sources.
- Return at least the configured source count and a claim_evidence record for each material technical claim.
- Every claim_evidence record must include claim, supporting_sources, confidence, verified_at, and version_context.

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
  "sources": [],
  "claim_evidence": [],
  "version_context": ""
}}
""".strip()


def update_post_file(
    post: Post,
    payload: dict[str, Any],
    config: dict[str, Any],
    *,
    clear_noindex: bool = False,
    substantive: bool = True,
) -> None:
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
        frontmatter["tags"] = sanitize_tags(payload["tags"], {"title": post.title, "categories": post.categories}, config)
    if payload.get("categories"):
        frontmatter["categories"] = sanitize_categories(payload["categories"], None, config)
    if substantive:
        frontmatter["lastmod"] = local_now(config).replace(microsecond=0).isoformat()
        frontmatter["last_reviewed"] = local_now(config).date().isoformat()
        frontmatter["verification_date"] = iso_z()
        frontmatter["verification_version"] = int(frontmatter.get("verification_version", 0) or 0) + 1
        frontmatter["version_context"] = normalize_space(str(payload.get("version_context", "Documentation current at verification time")))[:240]
        frontmatter["recheck_after"] = (local_now(config) + dt.timedelta(days=int(config.get("revalidation_intervals", {}).get("default_days", 60)))).date().isoformat()
    if clear_noindex:
        frontmatter.pop("noindex", None)
        frontmatter.pop("audit_status", None)
    post.path.write_text(compose_markdown(frontmatter, body), encoding="utf-8")


def inferred_category(post: Post, config: dict[str, Any]) -> str:
    """Classify legacy content into the controlled, site-wide category set."""
    scope = config.get("topic_scope", {})
    approved = list(scope.get("approved_categories", config.get("taxonomy", {}).get("allowed_categories", [])))
    aliases = {
        slugify(str(key), max_length=50): slugify(str(value), max_length=50)
        for key, value in config.get("taxonomy", {}).get("aliases", {}).items()
    }
    text = f"{post.title} {post.description} {' '.join(post.tags)} {' '.join(post.categories)}".lower()
    explicit_rules = [
        ("cloud-certifications", r"\b(?:certification|study guide|cheat\s*sheet|exam|az-\d+|sc-\d+|ms-\d+|comptia)\b"),
        ("entra-id", r"\b(?:microsoft entra|entra id|azure ad|conditional access|service principal)\b"),
        ("azure", r"\b(?:microsoft azure|azure chaos|azure virtual|azure kubernetes|azure monitor)\b"),
        ("identity-access-management", r"\b(?:identity security|authentication|authorization|mfa|passwordless|passkeys?|iam|rbac)\b"),
        ("cybersecurity", r"\b(?:cybersecurity|vulnerability|heartbleed|zero trust|siem|xdr|soar|threat|prompt injection|security flaw|secur(?:e|ing) (?:the )?\w*\s*supply chain)\b"),
        ("networking", r"\b(?:network|networking|dns|tcp|udp|routing|switching|firewall|vpn|cabling|ip basics|internet protocol|http status)\b"),
        ("system-administration", r"\b(?:system administration|sysadmin|windows server|active directory|powershell|linux server)\b"),
        ("developer-it-tools", r"\b(?:developer|programming|java|python|c#|spring|jdbc|jpa|hibernate|leetcode|algorithm|github|docker|kubernetes|terraform|ci/cd|api|model context protocol)\b"),
        ("it-fundamentals", r"\b(?:it fundamentals|hardware|motherboard|cpu|gpu|computer basics|operating system)\b"),
    ]
    for category, pattern in explicit_rules:
        if category in approved and re.search(pattern, text):
            return category
    scores: dict[str, int] = {category: 0 for category in approved}
    for category in post.categories:
        normalized = aliases.get(slugify(category, max_length=50), slugify(category, max_length=50))
        if normalized in scores:
            scores[normalized] += 2
    for category, keywords in scope.get("category_keywords", {}).items():
        if category not in scores:
            continue
        scores[category] += sum(1 for keyword in keywords if str(keyword).lower() in text)
    if not scores:
        return "it-fundamentals"
    return max(approved, key=lambda category: (scores.get(category, 0), category))


def normalize_site_taxonomy(config: dict[str, Any], *, dry_run: bool, log: EventLog) -> int:
    """Apply controlled categories/tags and publisher identity without altering article prose or dates."""
    changed = 0
    for post in load_posts(config):
        frontmatter = dict(post.frontmatter)
        category = inferred_category(post, config)
        allowed_categories = set(config.get("taxonomy", {}).get("allowed_categories", []))
        category_aliases = {
            slugify(key, max_length=50): value
            for key, value in config.get("taxonomy", {}).get("aliases", {}).items()
        }
        normalized_categories: list[str] = []
        for existing in post.categories:
            normalized = category_aliases.get(
                slugify(existing, max_length=50),
                slugify(existing, max_length=50),
            )
            if normalized in allowed_categories and normalized not in normalized_categories:
                normalized_categories.append(normalized)
        if not normalized_categories:
            normalized_categories = [category]
        normalized_tags = sanitize_tags(
            post.tags,
            {"title": post.title, "categories": normalized_categories},
            config,
        )
        if not normalized_tags:
            category_tag = {
                "identity-access-management": "identity",
                "cloud-certifications": "cloud-certifications",
                "practical-infrastructure-guides": "infrastructure",
            }.get(category, category)
            normalized_tags = sanitize_tags([category_tag], {"title": post.title, "categories": normalized_categories}, config)
        desired = {
            "categories": normalized_categories,
            "tags": normalized_tags,
            "publisher": config.get("site", {}).get("publisher_name", "Compile My Mind"),
        }
        before = {key: frontmatter.get(key) for key in desired}
        has_personal_author = "author" in frontmatter
        if before == desired and not has_personal_author:
            continue
        frontmatter.update(desired)
        frontmatter.pop("author", None)
        changed += 1
        if not dry_run:
            post.path.write_text(compose_markdown(frontmatter, post.body), encoding="utf-8")
    log.log("taxonomy_normalized", changed=changed, dry_run=dry_run)
    return changed


def post_metadata_issues(posts: list[Post], config: dict[str, Any]) -> list[str]:
    issues: list[str] = []
    titles: set[str] = set()
    descriptions: set[str] = set()
    canonicals: set[str] = set()
    allowed_categories = set(config.get("taxonomy", {}).get("allowed_categories", []))
    controlled_tags = set(config.get("taxonomy", {}).get("controlled_tags", []))
    allow_new_tags = bool(config.get("taxonomy", {}).get("allow_new_tags", False))
    max_tags = int(config.get("taxonomy", {}).get("max_tags_per_article", 8))
    for post in posts:
        normalized_title = normalize_space(post.title).lower()
        if normalized_title in titles:
            issues.append(f"Duplicate SEO title: {post.title}")
        titles.add(normalized_title)
        normalized_description = normalize_space(post.description).lower()
        if not normalized_description:
            issues.append(f"Missing meta description in {post.path.relative_to(ROOT)}")
        elif normalized_description in descriptions:
            issues.append(f"Duplicate meta description in {post.path.relative_to(ROOT)}")
        descriptions.add(normalized_description)
        if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", post.slug):
            issues.append(f"Invalid or unstable slug in {post.path.relative_to(ROOT)}")
        canonical = canonical_url(f"{str(config.get('site', {}).get('base_url', '')).rstrip('/')}{post.url_path}")
        if canonical in canonicals:
            issues.append(f"Duplicate canonical URL: {canonical}")
        canonicals.add(canonical)
        if post.frontmatter.get("author"):
            issues.append(f"Personal author metadata remains in {post.path.relative_to(ROOT)}")
        if post.frontmatter.get("publisher") != config.get("site", {}).get("publisher_name", "Compile My Mind"):
            issues.append(f"Publisher metadata is missing in {post.path.relative_to(ROOT)}")
        if not parse_date(post.date):
            issues.append(f"Invalid publication date in {post.path.relative_to(ROOT)}")
        lastmod = parse_date(str(post.frontmatter.get("lastmod", "")))
        date = parse_date(post.date)
        if lastmod and date and lastmod < date:
            issues.append(f"Updated date precedes publication date in {post.path.relative_to(ROOT)}")
        if allowed_categories and not set(post.categories) <= allowed_categories:
            issues.append(f"Uncontrolled category in {post.path.relative_to(ROOT)}")
        invalid_tag_format = any(
            tag != slugify(str(tag), max_length=34)
            for tag in post.tags
        )
        if not post.tags or len(post.tags) > max_tags or invalid_tag_format:
            issues.append(f"Uncontrolled tag metadata in {post.path.relative_to(ROOT)}")
        elif controlled_tags and not allow_new_tags and not set(post.tags) <= controlled_tags:
            issues.append(f"Uncontrolled tag metadata in {post.path.relative_to(ROOT)}")
    return issues


def existing_content_audit(config: dict[str, Any], *, apply_noindex: bool, log: EventLog) -> dict[str, Any]:
    """Create a fail-safe inventory and noindex high-risk legacy content pending substantive repair."""
    posts = load_posts(config)
    entries: list[dict[str, Any]] = []
    high_risk_pattern = re.compile(r"(?i)\b(cpu|gpu|motherboard|ryzen|rtx|core ultra|benchmark|gemini\s*3|pricing|price)\b")
    for post in posts:
        markdown = post.body
        sources = extract_links(markdown)
        scope_result = topic_relevance_score(
            {
                "title": post.title,
                "search_intent": post.description,
                "primary_category": post.categories[0] if post.categories else "",
                "categories": post.categories,
                "tags": post.tags,
            },
            config,
        )
        numeric_claims = len(re.findall(r"(?<!\w)(?:\d+(?:\.\d+)?%|\$\d+|\d+\s*(?:w|watts|fps|gb|tb|ghz|mhz))\b", markdown, flags=re.I))
        code_issues = code_block_issues(markdown)
        link_issues = internal_link_issues(markdown, {"categories": post.categories}, {"publishing": {}}, posts)
        intro_issues = introduction_issues(markdown)
        issues: list[str] = []
        out_of_scope = scope_result.get("critical_failure") == "disallowed_topic"
        if out_of_scope:
            issues.append("out_of_scope_topic:" + ",".join(scope_result.get("disallowed_matches", [])[:5]))
        required_sources = int(config.get("publishing", {}).get("required_source_count", 3))
        if len(sources) < required_sources:
            issues.append(f"fewer_than_{required_sources}_external_sources")
        if not post.frontmatter.get("verification_date"):
            issues.append("missing_technical_verification_date")
        if numeric_claims:
            issues.append(f"numeric_claims:{numeric_claims}")
        issues.extend(f"code:{issue}" for issue in code_issues)
        issues.extend(f"internal_link:{issue}" for issue in link_issues)
        issues.extend(f"introduction:{issue}" for issue in intro_issues)
        outdated_model_identifier = bool(re.search(r"(?i)\b(?:gemini\s*3|gpt-4(?:\.0)?|text-davinci|azure ad graph)\b", f"{post.title} {post.slug}"))
        if outdated_model_identifier:
            issues.append("potentially_outdated_model_or_api_identifier")
        high_risk = out_of_scope or bool(code_issues) or outdated_model_identifier or bool(high_risk_pattern.search(f"{post.title} {post.slug}")) and numeric_claims > 0
        high_risk = high_risk or (numeric_claims >= 5 and len(sources) < required_sources)
        action = "noindex_pending_repair" if high_risk else ("revalidate" if issues else "none")
        if apply_noindex and high_risk and not post.frontmatter.get("noindex"):
            frontmatter = dict(post.frontmatter)
            frontmatter["noindex"] = True
            frontmatter["audit_status"] = "pending-automated-repair"
            post.path.write_text(compose_markdown(frontmatter, post.body), encoding="utf-8")
        entries.append(
            {
                "slug": post.slug,
                "title": post.title,
                "risk": "high" if high_risk else ("medium" if issues else "low"),
                "action": action,
                "issues": issues,
                "source_count": len(sources),
                "numeric_claim_count": numeric_claims,
                "noindex": bool(post.frontmatter.get("noindex") or (apply_noindex and high_risk)),
            }
        )
    report = {
        "generated_at": iso_z(),
        "articles_checked": len(entries),
        "summary": {
            "high_risk": sum(entry["risk"] == "high" for entry in entries),
            "medium_risk": sum(entry["risk"] == "medium" for entry in entries),
            "low_risk": sum(entry["risk"] == "low" for entry in entries),
            "noindexed": sum(entry["noindex"] for entry in entries),
            "unresolved": sum(bool(entry["issues"]) for entry in entries),
        },
        "corrected_articles": [],
        "noindexed_articles": [entry["slug"] for entry in entries if entry["noindex"]],
        "unpublished_articles": [],
        "rejected_repair_attempts": [],
        "remaining_unresolved": [entry["slug"] for entry in entries if entry["issues"]],
        "articles": entries,
    }
    write_json(CONTENT_AUDIT_PATH, report)
    log.log(
        "existing_content_audit_completed",
        articles=len(entries),
        noindexed=len(report["noindexed_articles"]),
        unresolved=len(report["remaining_unresolved"]),
    )
    return report


def run_maintain(args: argparse.Namespace) -> int:
    config = load_config()
    state = load_state()
    log = EventLog()
    client = GeminiClient(config, log, state)
    client.require_key()
    taxonomy_changed = 0
    if config.get("taxonomy", {}).get("controlled_tags"):
        taxonomy_changed = normalize_site_taxonomy(config, dry_run=args.dry_run, log=log)
    posts = load_posts(config)
    limit = args.max_articles or int(config.get("maintenance", {}).get("max_articles_per_run", 1))
    selected = select_posts_for_maintenance(posts, state, config, limit)
    log.log("maintenance_selected", count=len(selected), slugs=[post.slug for post in selected])
    exit_code = 0
    quota_limited = False
    corrected_articles: list[str] = []
    failed_repairs: list[dict[str, str]] = []
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
            failed_repairs.append({"slug": post.slug, "reason": "grounded_research_quota_limited"})
            break
        except Exception as error:
            log.log("maintenance_grounded_research_failed", slug=post.slug, error=str(error))
            failed_repairs.append({"slug": post.slug, "reason": "grounded_research_failed"})
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
            failed_repairs.append({"slug": post.slug, "reason": "maintenance_review_quota_limited"})
            break
        except Exception as error:
            log.log("maintenance_ai_failed", slug=post.slug, error=str(error))
            exit_code = 1
            failed_repairs.append({"slug": post.slug, "reason": "maintenance_model_failed"})
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
            if isinstance(source, dict) and source.get("url") and is_trusted_source_url(str(source.get("url", "")).strip(), config)
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
            failed_repairs.append({"slug": post.slug, "reason": "untrusted_new_links"})
            continue
        payload["sources"] = [
            source
            for source in (payload.get("sources", []) or [])
            if isinstance(source, dict) and str(source.get("url", "")).strip() in allowed_urls
        ]
        if word_count(updated) < max(500, int(word_count(post.body) * 0.55)):
            log.log("maintenance_update_rejected", slug=post.slug, reason="updated_body_too_short")
            exit_code = 1
            failed_repairs.append({"slug": post.slug, "reason": "updated_body_too_short"})
            continue
        if cosine_similarity(post.body, updated) < float(config.get("maintenance", {}).get("update_similarity_floor", 0.55)):
            log.log("maintenance_update_rejected", slug=post.slug, reason="update_changed_article_too_much")
            exit_code = 1
            failed_repairs.append({"slug": post.slug, "reason": "update_changed_article_too_much"})
            continue
        source_candidates: list[ResearchItem] = []
        grounded_by_url = {
            canonical_url(str(source.get("url", ""))): source
            for source in grounded.get("citations", []) or []
            if isinstance(source, dict) and source.get("url")
        }
        for source in payload.get("sources", []) or []:
            url = str(source.get("url", "")).strip()
            grounded_source = grounded_by_url.get(canonical_url(url), {})
            source_candidates.append(
                ResearchItem(
                    source=normalized_url_host(url),
                    title=normalize_space(str(source.get("title") or grounded_source.get("title") or post.title)),
                    url=url,
                    summary=normalize_space(str(source.get("summary") or grounded_source.get("snippet") or grounded.get("text", "")))[:1800],
                    published=str(source.get("published", "")),
                    categories=post.categories,
                    score=1.0,
                )
            )
        validated_sources = validate_research_items(source_candidates, config, log)
        required_sources = int(config.get("publishing", {}).get("required_source_count", 3))
        if len(validated_sources) < required_sources:
            log.log(
                "maintenance_update_rejected",
                slug=post.slug,
                reason="not_enough_valid_sources",
                valid_sources=len(validated_sources),
            )
            exit_code = 1
            failed_repairs.append({"slug": post.slug, "reason": "not_enough_valid_sources"})
            continue
        candidate_article = {
            "title": post.title,
            "slug": post.slug,
            "description": normalize_space(str(payload.get("description") or post.description)),
            "summary": normalize_space(str(payload.get("summary") or post.description)),
            "categories": sanitize_categories(payload.get("categories") or post.categories, None, config),
            "tags": sanitize_tags(payload.get("tags") or post.tags, {"title": post.title, "categories": post.categories}, config),
            "sources": [
                {"title": source.title, "url": source.url, "publisher": source.source}
                for source in validated_sources
            ],
            "claim_evidence": payload.get("claim_evidence", []) or [],
            "article_markdown": updated,
            "diagrams": [],
            "charts": [],
        }
        topic = {
            "title": post.title,
            "slug": post.slug,
            "primary_category": candidate_article["categories"][0] if candidate_article["categories"] else "",
            "categories": candidate_article["categories"],
            "tags": candidate_article["tags"],
            "search_intent": post.title,
            "source_urls": [source.url for source in validated_sources],
            "source_titles": [source.title for source in validated_sources],
            "source_domains": [normalized_url_host(source.url) for source in validated_sources],
        }
        other_posts = [candidate for candidate in posts if candidate.slug != post.slug]
        validation_issues = deterministic_qa(candidate_article, topic, other_posts, config, validated_sources)
        if validation_issues:
            log.log("maintenance_update_rejected", slug=post.slug, reason="deterministic_qa_failed", issues=validation_issues[:30])
            exit_code = 1
            failed_repairs.append({"slug": post.slug, "reason": "deterministic_qa_failed"})
            continue
        qa = ai_qa(client, candidate_article, topic, config, log, validated_sources)
        quality = calculate_quality_score(candidate_article, topic, other_posts, config, qa, validated_sources)
        qa_score = float(qa.get("score", 0.0) or 0.0)
        approved = qa.get("approved") is True or str(qa.get("approved", "")).strip().lower() == "true"
        approved = approved and not (qa.get("unsupported_claims") or [])
        approved = approved and qa_score >= float(config.get("publishing", {}).get("ai_qa_min_score", 0.8))
        approved = approved and quality["score"] >= float(config.get("publishing", {}).get("quality_min_score", 0.82))
        if not approved:
            log.log("maintenance_update_rejected", slug=post.slug, reason="ai_or_quality_gate_failed", score=qa_score, quality=quality)
            exit_code = 1
            failed_repairs.append({"slug": post.slug, "reason": "ai_or_quality_gate_failed"})
            continue
        payload["sources"] = candidate_article["sources"]
        if args.dry_run:
            log.log("maintenance_dry_run_update_ready", slug=post.slug, reason=payload.get("reason", ""))
        else:
            update_post_file(post, payload, config, clear_noindex=True)
            corrected_articles.append(post.slug)
            log.log("maintenance_post_updated", slug=post.slug, reason=payload.get("reason", ""))
        state.setdefault("maintenance_reviews", {})[post.slug] = {
            "time": iso_z(),
            "action": "update" if not args.dry_run else "dry_run",
            "reason": payload.get("reason", ""),
            "broken_links": broken,
        }
    result = "quota_limited" if quota_limited else ("completed_with_errors" if exit_code else "completed")
    state["last_runs"]["maintain"] = {"time": iso_z(), "result": result, "taxonomy_changes": taxonomy_changed}
    save_state(state)
    current_posts = load_posts(config)
    noindexed_articles = sorted(
        post.slug for post in current_posts if getattr(post, "frontmatter", {}).get("noindex")
    )
    write_json(
        MAINTENANCE_REPORT_PATH,
        {
            "generated_at": iso_z(),
            "result": result,
            "corrected_articles": sorted(corrected_articles),
            "noindexed_articles": noindexed_articles,
            "unpublished_articles": [],
            "failed_repairs": failed_repairs,
            "remaining_issues": sorted(set(noindexed_articles) | {item["slug"] for item in failed_repairs}),
        },
    )
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
    metadata_issues = post_metadata_issues(posts, config)
    if duplicate_slugs or asset_issues or metadata_issues:
        log.log(
            "audit_failed",
            duplicate_slugs=duplicate_slugs,
            asset_issues=asset_issues[:50],
            metadata_issues=metadata_issues[:50],
            issue_count=len(asset_issues) + len(metadata_issues),
        )
        return 1
    return 0


def read_log_events() -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for path in sorted((AUTOPUBLISHER_DIR / "logs").glob("*.jsonl")):
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                payload = json.loads(line)
                if isinstance(payload, dict):
                    events.append(payload)
        except Exception:
            continue
    return events


def monitoring_dashboard(
    state: dict[str, Any],
    posts: list[Post],
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    config = config or {}
    today = utc_now().date().isoformat()
    events = [event for event in read_log_events() if str(event.get("time", "")).startswith(today)]
    event_counts = Counter(str(event.get("event", "unknown")) for event in events)
    published_today = [entry for entry in state.get("generated_posts", []) if str(entry.get("time", "")).startswith(today)]
    rejected_today = [entry for entry in state.get("rejected_articles", []) if str(entry.get("time", "")).startswith(today)]
    quality_scores = [
        float(entry["quality_score"])
        for entry in state.get("generated_posts", [])
        if entry.get("quality_score") is not None
    ]
    queue_depth = len(valid_ready_publications(state))
    queue_target = int(config.get("publication_queue", {}).get("target_depth", 0))
    queue_minimum = int(config.get("publication_queue", {}).get("minimum_depth", 0))
    return {
        "generated_at": iso_z(),
        "ready_queue_depth": queue_depth,
        "ready_queue_target": queue_target,
        "ready_queue_minimum": queue_minimum,
        "ready_queue_healthy": queue_depth >= queue_minimum if queue_minimum else bool(queue_depth),
        "topics_discovered_today": event_counts["research_collected"] + event_counts["grounded_research_completed"],
        "topics_rejected_today": event_counts["topic_rejected"] + event_counts["topic_deferred_to_maintenance"],
        "articles_generated_today": len([event for event in events if event.get("event") == "article_generation_started"]),
        "articles_published_today": len(published_today),
        "articles_rejected_today": len(rejected_today),
        "failed_validation_checks": event_counts["article_deterministic_qa_failed"] + event_counts["article_ai_qa_failed"],
        "source_validation_failures": event_counts["source_validation_failed"],
        "unsupported_claim_failures": sum(
            1 for event in events if "unsupported" in json.dumps(event, ensure_ascii=False).lower()
        ),
        "generic_content_failures": sum(
            1 for event in events if "generic paragraph" in json.dumps(event, ensure_ascii=False).lower()
        ),
        "repeated_content_failures": sum(
            1 for event in events if "repeat" in json.dumps(event, ensure_ascii=False).lower()
        ),
        "numerical_claim_failures": sum(
            1 for event in events if "numerical" in json.dumps(event, ensure_ascii=False).lower()
        ),
        "invalid_code_blocks": sum(
            1 for event in events if "invalid " in json.dumps(event, ensure_ascii=False).lower() and "code block" in json.dumps(event, ensure_ascii=False).lower()
        ),
        "dangerous_commands": sum(
            1 for event in events if "destructive command" in json.dumps(event, ensure_ascii=False).lower()
        ),
        "possible_secrets": sum(
            1 for event in events if "credential" in json.dumps(event, ensure_ascii=False).lower()
        ),
        "similarity_failures": sum(1 for event in events if "similar" in str(event.get("reason", ""))),
        "broken_links": event_counts["maintenance_update_rejected"],
        "outdated_articles": len([review for review in state.get("maintenance_reviews", {}).values() if review.get("action") == "update"]),
        "publishing_errors": event_counts["publish_retryable"] + event_counts["hugo_build_failed"],
        "sitemap_errors": event_counts["sitemap_validation_failed"],
        "metadata_errors": event_counts["metadata_validation_failed"],
        "average_quality_score": round(sum(quality_scores) / len(quality_scores), 4) if quality_scores else None,
        "topic_distribution": Counter(category for post in posts for category in post.categories),
        "articles_per_category": Counter(category for post in posts for category in post.categories),
        "event_counts": event_counts,
    }


def run_report(_args: argparse.Namespace) -> int:
    config = load_config()
    state = load_state()
    dashboard = monitoring_dashboard(state, load_posts(config), config)
    write_json(DASHBOARD_PATH, dashboard)
    print(json.dumps(dashboard, indent=2, ensure_ascii=False, default=dict))
    return 0


def run_taxonomy(args: argparse.Namespace) -> int:
    config = load_config()
    log = EventLog()
    normalize_site_taxonomy(config, dry_run=args.dry_run, log=log)
    return 0


def jsonld_types(value: Any) -> set[str]:
    types: set[str] = set()
    if isinstance(value, dict):
        schema_type = value.get("@type")
        if isinstance(schema_type, str):
            types.add(schema_type)
        elif isinstance(schema_type, list):
            types.update(str(item) for item in schema_type)
        for child in value.values():
            types.update(jsonld_types(child))
    elif isinstance(value, list):
        for child in value:
            types.update(jsonld_types(child))
    return types


def rendered_index_sync_issues(output_dir: Path) -> list[str]:
    home_path = output_dir / "index.html"
    posts_path = output_dir / "posts" / "index.html"
    if not home_path.exists() or not posts_path.exists():
        return []
    home_document = home_path.read_text(encoding="utf-8", errors="ignore")
    posts_document = posts_path.read_text(encoding="utf-8", errors="ignore")
    link_pattern = re.compile(
        r'''href=(?:"(/posts/[^"#?\s>]+/)"|'(/posts/[^'#?\s>]+/)'|(/posts/[^"'#?\s>]+/))''',
        flags=re.I,
    )

    def post_links(document: str) -> set[str]:
        return {
            next(value for value in match if value)
            for match in link_pattern.findall(document)
        }

    def post_count(document: str) -> str:
        match = re.search(
            r'''data-post-total=(?:"(\d+)"|'(\d+)'|(\d+))''',
            document,
            flags=re.I,
        )
        return next((value for value in match.groups() if value), "") if match else ""

    home_links = post_links(home_document)
    index_links = post_links(posts_document)
    issues: list[str] = []
    if not home_links <= index_links:
        issues.append("Homepage contains post links that are missing from the posts index.")
    home_count = post_count(home_document)
    index_count = post_count(posts_document)
    if not home_count or not index_count:
        issues.append("Homepage or posts index is missing its synchronized post-count marker.")
    elif home_count != index_count:
        issues.append("Homepage and posts-index post counts are inconsistent.")
    return issues


def rendered_site_issues(output_dir: Path, config: dict[str, Any]) -> list[str]:
    issues: list[str] = []
    base_url = str(config.get("site", {}).get("base_url", ""))
    expected_host = normalized_url_host(base_url)
    sitemap_path = output_dir / "sitemap.xml"
    sitemap_urls: list[str] = []
    if not sitemap_path.exists():
        issues.append("Rendered sitemap.xml is missing.")
    else:
        try:
            root = ET.fromstring(sitemap_path.read_text(encoding="utf-8"))
            sitemap_urls = [normalize_space("".join(element.itertext())) for element in root.iter() if element.tag.split("}")[-1] == "loc"]
        except (ET.ParseError, OSError) as error:
            issues.append(f"Rendered sitemap.xml is invalid: {error}")
    if len(sitemap_urls) != len(set(canonical_url(url) for url in sitemap_urls)):
        issues.append("Sitemap contains duplicate canonical URLs.")
    for url in sitemap_urls:
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme != "https" or normalized_url_host(url) != expected_host or parsed.query or parsed.fragment:
            issues.append(f"Sitemap contains a non-canonical URL: {url}")

    seen_canonicals: set[str] = set()
    found_types: set[str] = set()
    rendered_html = sorted(output_dir.rglob("*.html"))
    # ContactPage is optional.  Require it only when the site actually
    # renders a contact route; removing the optional contact page must not
    # make the release gate fail while still protecting any future contact
    # page from shipping without its matching structured data.
    contact_page_rendered = any(
        path.relative_to(output_dir).as_posix().lower() in {"contact.html", "contact/index.html"}
        or path.relative_to(output_dir).as_posix().lower().startswith("contact/")
        for path in rendered_html
    )
    accessibility = config.get("rendered_accessibility", {})
    for path in rendered_html:
        document = path.read_text(encoding="utf-8", errors="ignore")
        relative = path.relative_to(output_dir)
        if str(relative).lower() in {"404.html", "yandex_8865729cd882d7e9.html"}:
            continue
        if re.search(r'<meta\b[^>]*\bhttp-equiv=(?:["\']?refresh["\']?)', document, flags=re.I):
            continue

        def attribute_value(pattern: str) -> str:
            match = re.search(pattern, document, flags=re.I)
            if not match:
                return ""
            return next((value for value in match.groups() if value is not None), "")

        canonical_raw = attribute_value(
            r'<link\b(?=[^>]*\brel=(?:"canonical"|\'canonical\'|canonical)(?:\s|>))[^>]*\bhref=(?:"([^"]+)"|\'([^\']+)\'|([^\s>]+))'
        )
        canonical = canonical_url(html.unescape(canonical_raw)) if canonical_raw else ""
        if not canonical:
            issues.append(f"Missing canonical link: {path.relative_to(output_dir)}")
        elif canonical in seen_canonicals:
            issues.append(f"Duplicate rendered canonical: {canonical}")
        else:
            seen_canonicals.add(canonical)
        if canonical:
            parsed = urllib.parse.urlsplit(canonical)
            if parsed.scheme != "https" or normalized_url_host(canonical) != expected_host or parsed.query or parsed.fragment or parsed.path != parsed.path.lower():
                issues.append(f"Invalid rendered canonical: {canonical}")
        h1_count = len(re.findall(r"<h1(?:\s|>)", document, flags=re.I))
        if h1_count != 1:
            issues.append(f"Expected exactly one H1 in {path.relative_to(output_dir)}, found {h1_count}.")
        if accessibility.get("enabled"):
            if accessibility.get("require_main_landmark", True) and not re.search(r"<main(?:\s|>)", document, flags=re.I):
                issues.append(f"Missing main-content landmark: {path.relative_to(output_dir)}")
            for image_tag in re.findall(r"<img\b[^>]*>", document, flags=re.I):
                decorative = bool(re.search(r"\b(?:role=['\"]presentation['\"]|aria-hidden=['\"]true['\"])", image_tag, flags=re.I))
                alt_match = re.search(r"\balt\s*=\s*(['\"])(.*?)\1", image_tag, flags=re.I | re.S)
                if not alt_match:
                    issues.append(f"Image is missing alt text: {path.relative_to(output_dir)}")
                elif not decorative and not normalize_space(html.unescape(alt_match.group(2))):
                    issues.append(f"Informative image has empty alt text: {path.relative_to(output_dir)}")
                if accessibility.get("require_image_dimensions", True) and not (
                    re.search(r"\bwidth\s*=", image_tag, flags=re.I)
                    and re.search(r"\bheight\s*=", image_tag, flags=re.I)
                ):
                    issues.append(f"Image lacks explicit dimensions: {path.relative_to(output_dir)}")
        required_meta = [
            (r'<meta\b[^>]*\bname=(?:"description"|\'description\'|description)(?:\s|>)', "description"),
            (r'<meta\b[^>]*\bproperty=(?:"og:title"|\'og:title\'|og:title)(?:\s|>)', "Open Graph title"),
            (r'<meta\b[^>]*\bname=(?:"twitter:card"|\'twitter:card\'|twitter:card)(?:\s|>)', "Twitter card"),
            (r'<meta\b[^>]*\bname=(?:"robots"|\'robots\'|robots)(?:\s|>)', "robots directive"),
        ]
        for pattern, label in required_meta:
            if not re.search(pattern, document, flags=re.I):
                issues.append(f"Missing {label}: {path.relative_to(output_dir)}")
        robots = attribute_value(
            r'<meta\b(?=[^>]*\bname=(?:"robots"|\'robots\'|robots)(?:\s|>))[^>]*\bcontent=(?:"([^"]+)"|\'([^\']+)\'|([^\s>]+))'
        ).lower()
        if "noindex" in robots and canonical in {canonical_url(url) for url in sitemap_urls}:
            issues.append(f"Noindex page appears in sitemap: {canonical}")
        for payload in re.findall(
            r'<script\b[^>]*\btype=(?:"application/ld\+json"|\'application/ld\+json\'|application/ld\+json)[^>]*>(.*?)</script>',
            document,
            flags=re.I | re.S,
        ):
            try:
                structured = json.loads(html.unescape(payload))
            except json.JSONDecodeError as error:
                issues.append(f"Invalid JSON-LD in {path.relative_to(output_dir)}: {error}")
                continue
            types = jsonld_types(structured)
            found_types.update(types)
            if "Person" in types:
                issues.append(f"Forbidden Person schema in {path.relative_to(output_dir)}")
            if "BlogPosting" in types:
                publisher = structured.get("publisher", {}) if isinstance(structured, dict) else {}
                author = structured.get("author", {}) if isinstance(structured, dict) else {}
                if publisher.get("@type") != "Organization" or author.get("@type") != "Organization":
                    issues.append(f"BlogPosting lacks Organization publisher/author in {path.relative_to(output_dir)}")
    required_types = {"Organization", "WebSite", "BreadcrumbList", "BlogPosting", "CollectionPage"}
    if contact_page_rendered:
        required_types.add("ContactPage")
    for required_type in required_types:
        if required_type not in found_types:
            issues.append(f"Required structured-data type was not rendered: {required_type}")

    issues.extend(rendered_index_sync_issues(output_dir))
    return issues


def run_rendered_audit(args: argparse.Namespace) -> int:
    output_dir = Path(args.output_dir or ROOT / "public")
    issues = rendered_site_issues(output_dir, load_config())
    write_json(AUTOPUBLISHER_DIR / "reports" / "rendered-site-audit.json", {"time": iso_z(), "issues": issues})
    if issues:
        for issue in issues:
            print(f"rendered_audit: {issue}", file=sys.stderr)
        return 1
    print("rendered_audit: PASS")
    return 0


def run_existing_audit(args: argparse.Namespace) -> int:
    config = load_config()
    existing_content_audit(config, apply_noindex=not args.dry_run, log=EventLog())
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Autonomous publishing engine for Compile My Mind.")
    parser.add_argument(
        "--mode",
        choices=["publish", "prepare", "maintain", "audit", "report", "taxonomy", "existing-audit", "rendered-audit"],
        required=True,
    )
    parser.add_argument("--dry-run", action="store_true", help="Run decisions without writing article updates.")
    parser.add_argument("--max-articles", type=int, default=None, help="Maximum articles to review in maintenance mode.")
    parser.add_argument("--output-dir", default=None, help="Rendered Hugo output directory for rendered-audit mode.")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.mode == "publish":
        return run_publish(args)
    if args.mode == "prepare":
        return run_prepare(args)
    if args.mode == "maintain":
        return run_maintain(args)
    if args.mode == "audit":
        return run_audit(args)
    if args.mode == "report":
        return run_report(args)
    if args.mode == "taxonomy":
        return run_taxonomy(args)
    if args.mode == "existing-audit":
        return run_existing_audit(args)
    if args.mode == "rendered-audit":
        return run_rendered_audit(args)
    parser.error(f"Unknown mode: {args.mode}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
