#!/usr/bin/env python3
"""Source-first Gemini publishing pipeline for Compile My Mind.

The pipeline deliberately does not discover topics at runtime.  It consumes the
next brief from automation/content-pipeline.json, where each brief has a fixed
set of trusted primary sources.  A source outage or rejected draft is recorded
and skipped; it never creates a partial post or weakens the quality checks.
"""

from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = ROOT / "automation" / "content-pipeline.json"
STATE_PATH = ROOT / ".content-pipeline" / "state.json"
RESULT_PATH = ROOT / ".content-pipeline" / "result.json"
POSTS_PATH = ROOT / "content" / "posts"
GEMINI_API_ROOT = "https://generativelanguage.googleapis.com/v1beta"
USER_AGENT = "CompileMyMindContentPipeline/1.0 (+https://compilemymind.com)"


class PipelineError(RuntimeError):
    """A deterministic configuration or content-pipeline error."""


class SourceUnavailable(PipelineError):
    """A configured source could not be fetched safely."""


class GeminiUnavailable(PipelineError):
    """Gemini could not produce a usable response for this run."""


@dataclass(frozen=True)
class Source:
    title: str
    url: str
    excerpt: str


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def emit(event: str, **fields: Any) -> None:
    print(json.dumps({"time": utc_now(), "event": event, **fields}, ensure_ascii=False))


def read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def canonical_url(value: str) -> str:
    parsed = urllib.parse.urlsplit(value.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return ""
    query = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    retained = [(key, item) for key, item in query if not key.lower().startswith("utm_")]
    return urllib.parse.urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), parsed.path.rstrip("/") or "/", urllib.parse.urlencode(retained), ""))


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug[:80].rstrip("-")


def normalize_space(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def strip_html(document: str) -> str:
    document = re.sub(r"(?is)<(script|style|noscript).*?>.*?</\1>", " ", document)
    document = re.sub(r"(?is)<[^>]+>", " ", document)
    return normalize_space(html.unescape(document))


def markdown_quote(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def load_config() -> dict[str, Any]:
    config = read_json(CONFIG_PATH, {})
    issues = config_issues(config)
    if issues:
        raise PipelineError("Invalid automation/content-pipeline.json: " + "; ".join(issues))
    return config


def config_issues(config: dict[str, Any]) -> list[str]:
    issues: list[str] = []
    if config.get("schema_version") != 1:
        issues.append("schema_version must be 1")
    if not isinstance(config.get("model"), str) or not config["model"].strip():
        issues.append("model is required")
    trusted = {str(item).lower().strip() for item in config.get("trusted_domains", []) if str(item).strip()}
    if not trusted:
        issues.append("trusted_domains must not be empty")
    article = config.get("article") or {}
    if int(article.get("minimum_words", 0)) < 600:
        issues.append("article.minimum_words must be at least 600")
    if int(article.get("minimum_sections", 0)) < 2:
        issues.append("article.minimum_sections must be at least 2")
    briefs = config.get("briefs")
    if not isinstance(briefs, list) or not briefs:
        issues.append("briefs must contain at least one source-backed brief")
        return issues
    seen_slugs: set[str] = set()
    for index, brief in enumerate(briefs, start=1):
        label = f"brief {index}"
        if not isinstance(brief, dict):
            issues.append(f"{label} must be an object")
            continue
        slug = str(brief.get("slug", ""))
        if not slug or slugify(slug) != slug:
            issues.append(f"{label} has an invalid slug")
        elif slug in seen_slugs:
            issues.append(f"{label} repeats slug {slug}")
        seen_slugs.add(slug)
        for field in ("title", "description", "reader_goal"):
            if not normalize_space(str(brief.get(field, ""))):
                issues.append(f"{label} is missing {field}")
        if not isinstance(brief.get("categories"), list) or not brief["categories"]:
            issues.append(f"{label} needs at least one category")
        sources = brief.get("sources")
        if not isinstance(sources, list) or len(sources) < 3:
            issues.append(f"{label} needs at least three sources")
            continue
        urls: set[str] = set()
        for source in sources:
            if not isinstance(source, dict):
                issues.append(f"{label} has an invalid source")
                continue
            url = canonical_url(str(source.get("url", "")))
            host = urllib.parse.urlsplit(url).hostname or ""
            if not normalize_space(str(source.get("title", ""))) or not url:
                issues.append(f"{label} has a source without title or URL")
            elif host.lower() not in trusted and not any(host.lower().endswith("." + domain) for domain in trusted):
                issues.append(f"{label} source host is not trusted: {host}")
            elif url in urls:
                issues.append(f"{label} repeats source {url}")
            urls.add(url)
    return issues


def load_state() -> dict[str, Any]:
    state = read_json(STATE_PATH, {"version": 1, "completed_slugs": [], "attempts": []})
    if not isinstance(state, dict):
        return {"version": 1, "completed_slugs": [], "attempts": []}
    state.setdefault("version", 1)
    state.setdefault("completed_slugs", [])
    state.setdefault("attempts", [])
    return state


def save_state(state: dict[str, Any]) -> None:
    state["attempts"] = list(state.get("attempts", []))[-80:]
    state["completed_slugs"] = list(dict.fromkeys(str(item) for item in state.get("completed_slugs", [])))
    write_json(STATE_PATH, state)


def record(state: dict[str, Any], slug: str, result: str, reason: str = "") -> None:
    state.setdefault("attempts", []).append({"time": utc_now(), "slug": slug, "result": result, "reason": reason[:600]})
    save_state(state)


def write_result(result: str, **fields: Any) -> None:
    write_json(RESULT_PATH, {"time": utc_now(), "result": result, **fields})


def existing_slugs() -> set[str]:
    if not POSTS_PATH.exists():
        return set()
    return {path.name for path in POSTS_PATH.iterdir() if path.is_dir() and (path / "index.md").exists()}


def pending_briefs(config: dict[str, Any], state: dict[str, Any]) -> list[dict[str, Any]]:
    completed = {str(item) for item in state.get("completed_slugs", [])}
    completed.update(existing_slugs())
    return [brief for brief in config["briefs"] if brief["slug"] not in completed]


def fetch_source(source: dict[str, Any]) -> Source:
    url = canonical_url(str(source["url"]))
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"})
    try:
        with urllib.request.urlopen(request, timeout=25) as response:
            status = getattr(response, "status", 200)
            body = response.read(220_000)
            content_type = response.headers.get("Content-Type", "")
    except urllib.error.HTTPError as error:
        raise SourceUnavailable(f"{url} returned HTTP {error.code}") from error
    except urllib.error.URLError as error:
        raise SourceUnavailable(f"{url} could not be reached: {error.reason}") from error
    if status >= 400:
        raise SourceUnavailable(f"{url} returned HTTP {status}")
    if "html" not in content_type.lower() and "text" not in content_type.lower():
        raise SourceUnavailable(f"{url} did not return readable documentation")
    document = body.decode("utf-8", errors="ignore")
    excerpt = strip_html(document)
    if len(excerpt) < 250:
        raise SourceUnavailable(f"{url} did not provide enough readable text")
    title_match = re.search(r"(?is)<title[^>]*>(.*?)</title>", document)
    title = normalize_space(strip_html(title_match.group(1))) if title_match else normalize_space(str(source["title"]))
    return Source(title=title[:180] or str(source["title"]), url=url, excerpt=excerpt[:2800])


def extract_json(text: str) -> dict[str, Any]:
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.I)
    try:
        value = json.loads(cleaned)
    except json.JSONDecodeError:
        decoder = json.JSONDecoder()
        starts = [index for index, char in enumerate(cleaned) if char == "{"]
        value = None
        for start in starts:
            try:
                candidate, _ = decoder.raw_decode(cleaned[start:])
            except json.JSONDecodeError:
                continue
            if isinstance(candidate, dict):
                value = candidate
                break
        if value is None:
            raise GeminiUnavailable("Gemini did not return a JSON object")
    if not isinstance(value, dict):
        raise GeminiUnavailable("Gemini JSON root must be an object")
    return value


def call_gemini(config: dict[str, Any], prompt: str) -> dict[str, Any]:
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key:
        raise PipelineError("GEMINI_API_KEY is required for publish mode")
    model = (os.environ.get("GEMINI_MODEL", "").strip() or str(config["model"])).strip()
    if not model:
        raise PipelineError("A Gemini model must be configured")
    request_url = f"{GEMINI_API_ROOT}/models/{urllib.parse.quote(model)}:generateContent"
    payload = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.25, "maxOutputTokens": 12_000, "responseMimeType": "application/json"},
    }
    request = urllib.request.Request(
        request_url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "x-goog-api-key": api_key},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=180) as response:
            raw = response.read().decode("utf-8")
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", errors="ignore")[:500]
        raise GeminiUnavailable(f"Gemini HTTP {error.code}: {detail}") from error
    except urllib.error.URLError as error:
        raise GeminiUnavailable(f"Gemini could not be reached: {error.reason}") from error
    try:
        response = json.loads(raw)
        text = "\n".join(
            str(part.get("text", ""))
            for candidate in response.get("candidates", [])
            for part in candidate.get("content", {}).get("parts", [])
            if isinstance(part, dict)
        ).strip()
    except (AttributeError, json.JSONDecodeError) as error:
        raise GeminiUnavailable("Gemini returned an invalid API response") from error
    if not text:
        raise GeminiUnavailable("Gemini returned no article text")
    return extract_json(text)


def article_prompt(brief: dict[str, Any], sources: list[Source], config: dict[str, Any]) -> str:
    evidence = [
        {"title": source.title, "url": source.url, "excerpt": source.excerpt}
        for source in sources
    ]
    return f"""
Write an original, practical technical article for Compile My Mind.

Editorial brief:
{json.dumps({key: brief[key] for key in ('title', 'description', 'reader_goal', 'categories', 'tags')}, ensure_ascii=False, indent=2)}

Use only the supplied source material for technical claims. Do not invent product behavior, configuration fields, security outcomes, version support, or commands. Explain uncertainty when the source material does not establish a point. Do not copy sentences from a source. Include safe, clearly labeled examples where they help readers, and warn before any action that changes a system.

The article body must have at least {config['article']['minimum_words']} words, at least {config['article']['minimum_sections']} H2 sections, and one useful Markdown table. Do not include a Sources or References heading; the pipeline adds the canonical source list itself.

Return JSON only:
{{
  "description": "105-180 character SEO description",
  "summary": "one-paragraph reader summary",
  "body_markdown": "complete Markdown article body"
}}

Verified source material:
{json.dumps(evidence, ensure_ascii=False, indent=2)}
""".strip()


def word_count(markdown: str) -> int:
    return len(re.findall(r"\b[\w][\w'-]*\b", markdown))


def article_issues(body: str, config: dict[str, Any]) -> list[str]:
    issues: list[str] = []
    minimum_words = int(config["article"]["minimum_words"])
    sections = len(re.findall(r"(?m)^##\s+\S", body))
    if word_count(body) < minimum_words:
        issues.append(f"article has {word_count(body)} words; expected at least {minimum_words}")
    if sections < int(config["article"]["minimum_sections"]):
        issues.append(f"article has {sections} H2 sections; expected at least {config['article']['minimum_sections']}")
    if config["article"].get("require_table") and not re.search(r"(?m)^\|(?:\s*:?-{3,}:?\s*\|){2,}\s*$", body):
        issues.append("article is missing a Markdown table")
    untyped_fences = re.findall(r"(?m)^```\s*$", body)
    if untyped_fences:
        issues.append("article contains a code fence without a language")
    if re.search(r"(?i)\b(as an ai|i cannot|i'm unable)\b", body):
        issues.append("article contains model-disclaimer text")
    return issues


def compose_post(brief: dict[str, Any], article: dict[str, Any], sources: list[Source]) -> str:
    description = normalize_space(str(article.get("description", "")))[:180] or brief["description"]
    summary = normalize_space(str(article.get("summary", "")))[:320] or brief["description"]
    body = str(article.get("body_markdown", "")).strip()
    source_list = "\n".join(f"- [{source.title}]({source.url})" for source in sources)
    now = utc_now()
    frontmatter = [
        "---",
        f"title: {markdown_quote(brief['title'])}",
        f"date: {markdown_quote(now)}",
        f"lastmod: {markdown_quote(now)}",
        f"description: {markdown_quote(description)}",
        f"summary: {markdown_quote(summary)}",
        "categories:",
        *[f"  - {markdown_quote(str(category))}" for category in brief["categories"]],
        "tags:",
        *[f"  - {markdown_quote(str(tag))}" for tag in brief.get("tags", [])],
        'publisher: "Compile My Mind"',
        "draft: false",
        "source_first: true",
        "---",
        "",
    ]
    return "\n".join(frontmatter) + body.rstrip() + "\n\n## Sources\n\n" + source_list + "\n"


def write_post(brief: dict[str, Any], document: str) -> Path:
    directory = POSTS_PATH / brief["slug"]
    if directory.exists():
        raise PipelineError(f"Post directory already exists: {directory.relative_to(ROOT)}")
    directory.mkdir(parents=True)
    path = directory / "index.md"
    path.write_text(document, encoding="utf-8")
    return path


def remove_post(path: Path) -> None:
    if path.parent.exists():
        shutil.rmtree(path.parent)


def hugo_build() -> tuple[bool, str]:
    hugo = shutil.which("hugo")
    if not hugo:
        return False, "Hugo is not available on PATH"
    result = subprocess.run([hugo, "--gc", "--minify"], cwd=ROOT, text=True, capture_output=True, timeout=180)
    if result.returncode:
        return False, (result.stderr or result.stdout)[-2000:]
    return True, ""


def publish() -> int:
    try:
        config = load_config()
    except PipelineError as error:
        write_result("configuration_error", detail=str(error))
        print(f"pipeline: {error}", file=sys.stderr)
        return 2
    if not os.environ.get("GEMINI_API_KEY", "").strip():
        write_result("configuration_error", detail="GEMINI_API_KEY is required")
        print("pipeline: GEMINI_API_KEY is required for publish mode", file=sys.stderr)
        return 2

    state = load_state()
    candidates = pending_briefs(config, state)
    if not candidates:
        record(state, "", "skipped_no_pending_brief", "Every configured brief has already been published.")
        write_result("skipped_no_pending_brief")
        emit("publish_skipped", reason="no_pending_brief")
        return 0

    for brief in candidates:
        slug = brief["slug"]
        emit("brief_started", slug=slug, title=brief["title"])
        try:
            sources = [fetch_source(source) for source in brief["sources"]]
        except SourceUnavailable as error:
            record(state, slug, "source_unavailable", str(error))
            emit("brief_skipped", slug=slug, reason="source_unavailable", detail=str(error))
            continue
        try:
            article = call_gemini(config, article_prompt(brief, sources, config))
        except GeminiUnavailable as error:
            record(state, slug, "gemini_unavailable", str(error))
            write_result("retryable_provider_error", slug=slug, detail=str(error))
            emit("publish_retryable", slug=slug, stage="article_generation", detail=str(error))
            return 0
        body = str(article.get("body_markdown", "")).strip()
        issues = article_issues(body, config)
        if issues:
            record(state, slug, "rejected_quality", "; ".join(issues))
            emit("brief_skipped", slug=slug, reason="rejected_quality", issues=issues)
            continue
        document = compose_post(brief, article, sources)
        try:
            path = write_post(brief, document)
        except PipelineError as error:
            record(state, slug, "write_conflict", str(error))
            emit("brief_skipped", slug=slug, reason="write_conflict", detail=str(error))
            continue
        passed, detail = hugo_build()
        if not passed:
            remove_post(path)
            record(state, slug, "build_failed", detail)
            emit("brief_skipped", slug=slug, reason="build_failed", detail=detail)
            continue
        state.setdefault("completed_slugs", []).append(slug)
        record(state, slug, "published")
        write_result("published", slug=slug, path=str(path.relative_to(ROOT)))
        emit("article_published", slug=slug, path=str(path.relative_to(ROOT)))
        return 0

    write_result("skipped_all_briefs_failed", attempted=[brief["slug"] for brief in candidates])
    emit("publish_skipped", reason="all_pending_briefs_failed", count=len(candidates))
    return 0


def validate() -> int:
    try:
        config = load_config()
    except PipelineError as error:
        print(f"pipeline: {error}", file=sys.stderr)
        return 2
    print(f"content_pipeline: PASS ({len(config['briefs'])} source-backed briefs)")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=["publish", "validate"], required=True)
    args = parser.parse_args(argv)
    return publish() if args.mode == "publish" else validate()


if __name__ == "__main__":
    raise SystemExit(main())
